/*!
 * \file PlaybackFrameRange.h
 * \brief Small playback frame/cut-range normalization helpers.
 */

#ifndef PLAYBACKFRAMERANGE_H
#define PLAYBACKFRAMERANGE_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>

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

// CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass contact sheet grabs the first frame PRESENTED by the
// measured Play at or after each target time, measured from the Play's own start. Targets are the
// centres of frameCount equal slices of the play window, so each lies strictly inside it whatever fps
// the leg reaches (a frame-number target past the last frame a slow leg reaches would never fire, and
// there is no replay to catch it). 0 when frameCount <= 0.
inline long long contactSheetInPassTargetMs( int i, int frameCount, long long windowMs )
{
    if( frameCount <= 0 || windowMs <= 0 ) return 0;
    return static_cast<long long>( std::llround(
        ( 2.0 * i + 1.0 ) * static_cast<double>( windowMs ) / ( 2.0 * frameCount ) ) );
}

// The in-pass grabs run on the GUI thread inside the measured interval. The honest companion to the
// leg's fps is the fps with their recorded cost taken out of the elapsed time: equal to the measured
// fps when nothing was grabbed, never lower. A grab total that swallows the interval is a broken
// record and yields 0, never an infinite fps.
inline double fpsExcludingGrabCost( long long presentedFrames, double elapsedMs, double grabTotalMs )
{
    const double usableMs = elapsedMs - std::max( 0.0, grabTotalMs );
    if( presentedFrames <= 0 || elapsedMs <= 0.0 || usableMs <= 0.0 ) return 0.0;
    return static_cast<double>( presentedFrames ) * 1000.0 / usableMs;
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
constexpr int kMinPlayWindowMs = 20000;   // kMinPlayWindowSeconds, in the milliseconds the wait loops count

// ENFORCE-2 round 2 (hub ruling): the window the caller REQUESTS -- the time its own stop timer / exercise loop
// lets Play run -- must itself reach the floor; having 20 s of footage available is not enough. Every
// automation mode that ends Play on its own clock asks for the window it will really hold.
//
// smokePlayRequestSeconds: the GUI smoke's Play ends at the SOONER of its --seconds timeout and its
// --presented-frames target (N / fps), so that is the window it requests. Unknown fps with a target fails
// closed to 0 s, which the floor refuses. The timeout keeps its pre-existing 100 ms minimum.
inline double smokePlayRequestSeconds( int durationMs, int targetPresentedFrames, double fps )
{
    const double timeoutSeconds = std::max( 100, durationMs ) / 1000.0;
    if( targetPresentedFrames <= 0 ) return timeoutSeconds;
    if( !( fps > 0.0 ) ) return 0.0;
    return std::min( timeoutSeconds, static_cast<double>( targetPresentedFrames ) / fps );
}

// The lifecycle stress switch STOPS Play on the first clip, so it may only happen once Play has run the floor.
inline bool stressSwitchReachesFloor( int switchAtMs ) { return switchAtMs >= kMinPlayWindowMs; }

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-ENFORCE-3 (owner rule 2026-10-01): "20 s of real footage" is a SOURCE-FRAME quantity.
//
// ENFORCE-2 measured "20 s" three different ways: admission with the clip's native fps, the engine with
// getFramerate() (a persisted fpsOverride changes it), and every automation STOP with a wall clock. With
// fpsOverride=12 on a 24 fps clip a run stopped after 20 s of wall clock having covered ~10 s of footage.
// Now the engine COUNTS the distinct source frames it has advanced (SourceFrameAdvanceCounter, fed from the
// engine tick), admission demands a window holding ceil(20 x native fps) of them that the engine can consume at
// its REAL pace, and every automation stop waits for the count (evaluatePlayStop). A wall clock survives only
// as a safety net whose expiry is a typed FAILURE, never a pass. Wrap / backward steps are never counted.
// ---------------------------------------------------------------------------------------------------------

// ceil(seconds x fps): the number of source frames that make `seconds` of footage. 0 for an unknown fps or
// window, which can never be "reached" (fail closed).
inline int64_t requiredSourceFrames( double nativeFps, double footageSpan )
{
    if( !( nativeFps > 0.0 ) || !( footageSpan > 0.0 ) ) return 0;
    return static_cast<int64_t>( std::ceil( footageSpan * nativeFps - 1e-9 ) );
}

// SourceFrameAdvanceCounter -- monotonic, process-cumulative (the FIRST Play start arms it; a later start never
// restarts it, so a replay cannot launder the count). consumed() is the number of DISTINCT source frames the
// engine has put the playhead on since the measured Play began, the start frame included: n frames are n/fps
// seconds of footage, and the range [position, cut-out] holds exactly (cut-out - position + 1) of them.
struct SourceFrameAdvanceCounter
{
    bool armed = false;
    int64_t startFrame = 0;
    int64_t highWater = 0;            // highest frame index reached; frames at or below it are never counted again
    int64_t forwardSteps = 0;         // distinct frames advanced beyond the start frame
    int wrapOrBackwardTicks = 0;      // ticks the engine moved backwards / wrapped: never counted as footage
    int beginsIgnored = 0;
    int externalJumpRebases = 0;      // the playhead was moved by something other than an engine tick (a seek)

    void begin( int64_t position )
    {
        if( armed ) { ++beginsIgnored; return; }
        armed = true;
        startFrame = highWater = position;
    }
    // One engine tick: the playhead moved from oldPosition to newPosition (fractional in drop-frame mode).
    void noteEngineTick( double oldPosition, double newPosition, bool wrapped )
    {
        if( !armed ) return;
        if( wrapped || newPosition < oldPosition ) { ++wrapOrBackwardTicks; return; }
        const int64_t oldFrame = static_cast<int64_t>( std::floor( oldPosition + 1e-9 ) );
        const int64_t newFrame = static_cast<int64_t>( std::floor( newPosition + 1e-9 ) );
        // The playhead sat ABOVE every frame this counter has seen when the tick began: something other than the
        // engine moved it (a seek, a snap at Play start). That jump is not footage played, so the high-water mark is
        // REBASED to where the playhead really was and the jump itself is never counted -- but the frames the engine
        // really advanced before it stay counted (r2, sol H1): consumed() is monotonic and never decreases.
        if( oldFrame > highWater )
        {
            ++externalJumpRebases;
            highWater = oldFrame;
        }
        if( newFrame > highWater )
        {
            forwardSteps += newFrame - highWater;
            highWater = newFrame;
        }
    }
    int64_t consumed() const { return armed ? forwardSteps + 1 : 0; }
};

// The wall-clock safety net every automation wait carries. It is NOT the stop: running into it is a typed
// failure (PLAY_SAFETY_TIMEOUT). It SCALES with the pace the venue must sustain (r2, fable H1): the engine is only
// required to hold kMinSustainedPaceFraction of the clip's native fps, so the budget for `requested` seconds of
// footage is requested / kMinSustainedPaceFraction + kPlaySafetyMarginMs. A venue below that floor is refused up front
// when its engine pace is known (evaluatePlayableWindow: PLAY_PACE_TOO_SLOW) and ends early with the SAME typed token
// when the pace only shows once Play runs (playPaceProjectsPastBudget) -- never a run that can only time out.
// The runner's own process budget (gui-smoke-length-gate.ps1 Get-GuiSmokePlaySafetyMs) uses the same formula.
constexpr double kMinSustainedPaceFraction = 0.5;
constexpr int kPlaySafetyMarginMs = 15000;
constexpr int64_t kPaceProbeMs = 8000;   // the measured pace is only trusted after this much Play wall clock

// CPU-LOOK-LEG-PACE-ABORT-1: a CPU-backend leg's frame rate is informational (docs/dual-venue-evidence.md), so its pace
// is MEASURED, never gated. PlayPaceMode::MeasuredInformational is armed only by the launcher's MLVAPP_PLAY_PACE_MODE
// =informational (the dual-venue CPU job); every other run -- CUDA legs, the interactive app, every other automation --
// is Gated and byte-for-byte what it was. Informational: the in-Play pace probe never ends the run, and the wall budget
// is the CPU CEILING requested / kCpuInformationalMinPaceFraction + kPlaySafetyMarginMs (25 s of footage: 765 s =
// 12.75 min). Reaching the ceiling first is the typed PLAY_SAFETY_TIMEOUT, never a hang. The 20 s window, the
// source-frame count, no-loop / no-replay, the run nonce and settings isolation are not touched by the mode.
// tools/profiling/gui-smoke-length-gate.ps1 mirrors the fraction (a parity test compares them).
enum class PlayPaceMode { Gated, MeasuredInformational };
constexpr double kCpuInformationalMinPaceFraction = 1.0 / 30.0;
inline double minSustainedPaceFractionFor( PlayPaceMode mode )
{
    return mode == PlayPaceMode::MeasuredInformational ? kCpuInformationalMinPaceFraction : kMinSustainedPaceFraction;
}
// Fail closed: only the exact word switches the gate off; unset, empty or anything else stays Gated.
inline PlayPaceMode playPaceModeFromEnvironmentValue( const char *value )
{
    return value && std::string( value ) == "informational" ? PlayPaceMode::MeasuredInformational : PlayPaceMode::Gated;
}
inline int64_t playSafetyMs( double requestedSeconds, PlayPaceMode mode = PlayPaceMode::Gated )
{
    return static_cast<int64_t>( std::max( 0.0, requestedSeconds ) / minSustainedPaceFractionFor( mode ) * 1000.0 )
         + kPlaySafetyMarginMs;
}

// The pace actually measured so far: source-frame steps advanced per second of Play wall clock. 0 when not measurable
// (fewer than two frames or no elapsed time). A CPU leg records it; nothing gates on it.
inline double measuredPaceFps( int64_t consumed, int64_t elapsedMs )
{
    if( consumed < 2 || elapsedMs <= 0 ) return 0.0;
    return static_cast<double>( consumed - 1 ) * 1000.0 / static_cast<double>( elapsedMs );
}

// True when the pace MEASURED so far (consumed frames over elapsed Play wall clock) cannot reach `required` frames
// inside the safety budget: the run is already doomed, so it ends now with PLAY_PACE_TOO_SLOW instead of waiting for
// the timeout. Needs at least two frames (one step) and kPaceProbeMs of Play, so a slow first frame is not a verdict.
inline bool playPaceProjectsPastBudget( int64_t consumed, int64_t required, int64_t elapsedMs, int64_t safetyMs )
{
    if( required <= 1 || consumed < 2 || elapsedMs < kPaceProbeMs ) return false;
    const double stepsDone = static_cast<double>( consumed - 1 );
    const double stepsNeeded = static_cast<double>( required - 1 );
    const double projectedMs = static_cast<double>( elapsedMs ) * stepsNeeded / stepsDone;
    return projectedMs > static_cast<double>( safetyMs );
}

enum class PlayStopState { Continue, Reached, EndedEarly, SafetyTimeout, PaceTooSlow };

// The ONE decision every automation Play wait makes. Reached (the source frames were consumed) wins over
// everything, including a Play that ended on that very frame; a Play that ended first is EndedEarly; a wall
// clock that ran out first is SafetyTimeout; a pace that cannot make it in time is PaceTooSlow. required <= 0 can
// never be reached.
inline PlayStopState evaluatePlayStop( int64_t consumed, int64_t required, bool playStillRunning,
                                       int64_t elapsedMs, int64_t safetyMs,
                                       PlayPaceMode mode = PlayPaceMode::Gated )
{
    if( required > 0 && consumed >= required ) return PlayStopState::Reached;
    if( !playStillRunning ) return PlayStopState::EndedEarly;
    if( elapsedMs >= safetyMs ) return PlayStopState::SafetyTimeout;
    // CPU-LOOK-LEG-PACE-ABORT-1: the pace probe is the Gated mode's only; an informational run measures its pace and
    // runs on to the requirement or to the (ceiling) safety timeout above.
    if( mode == PlayPaceMode::Gated
        && playPaceProjectsPastBudget( consumed, required, elapsedMs, safetyMs ) ) return PlayStopState::PaceTooSlow;
    return PlayStopState::Continue;
}

inline const char *playStopFailureReason( PlayStopState state )
{
    switch( state )
    {
    case PlayStopState::Reached: return "";
    case PlayStopState::EndedEarly: return "SOURCE_FRAMES_SHORT";
    case PlayStopState::SafetyTimeout: return "PLAY_SAFETY_TIMEOUT";
    case PlayStopState::PaceTooSlow: return "PLAY_PACE_TOO_SLOW";
    case PlayStopState::Continue: break;
    }
    return "PLAY_STATE_UNRESOLVED";
}

struct PlayableWindowVerdict
{
    bool ok = false;
    // "" when ok; otherwise a typed reason: CLIP_LENGTH_UNKNOWN | CLIP_TOO_SHORT | PLAY_DURATION_TOO_SHORT |
    // REPLAY_REFUSED.
    const char *reason = "CLIP_LENGTH_UNKNOWN";
    // "clip" when the whole clip is shorter than the requirement, "cut_range" when the clip is long enough
    // but the span from the current position to the cut-out is not, "requested" when the caller's own play
    // window is under the floor. Empty when ok / unknown.
    const char *scope = "";
    double clipSeconds = 0.0;
    double playableSeconds = 0.0;
    double requiredSeconds = kMinPlayWindowSeconds;
    double requestedSeconds = 0.0;   // the window the caller asked to hold Play for
    int positionFrame = 0;     // 0-based, clamped
    int lastPlayableFrame = 0; // 0-based inclusive: the cut-out frame
    int playableFrames = 0;    // frames positionFrame..lastPlayableFrame inclusive
    // ENFORCE-3: the same window in the unit the engine counts.
    int64_t requiredFrames = 0;        // ceil(max(floor, requested) x NATIVE fps)
    double paceFps = 0.0;              // the engine's real pace (getFramerate(): a persisted override included)
    double wallNeededSeconds = 0.0;    // wall clock the engine needs for requiredFrames at paceFps
    double wallBudgetSeconds = 0.0;    // wall clock the caller's safety net allows
};

// paceFps default: "the engine runs at the clip's native fps" (tests, and callers with no override).
constexpr double kPaceIsNative = 0.0;

// engineCutRangeForPlay -- the range the engine ACTUALLY plays. With the collapsed-range repair enabled (the
// normal state) it is normalizeCutRange(..., repair = true). MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR switches the
// engine's play path to leave the spin boxes untouched (normalizePlaybackCutRangeForLoadedClip returns early),
// so playbackHandling then stops at slider >= the RAW cut-out - 1: no widening of a one-frame or inverted
// range, only the clip end clamps it.
inline CutRange engineCutRangeForPlay( int cutIn, int cutOut, int totalFrames, bool repairEnabled )
{
    if( repairEnabled ) return normalizeCutRange( cutIn, cutOut, totalFrames, true );
    CutRange raw;
    if( totalFrames <= 0 ) return raw;
    raw.valid = true;
    raw.cutIn = std::max( 1, std::min( cutIn, totalFrames ) );
    raw.cutOut = std::max( 1, std::min( cutOut, totalFrames ) );
    return raw;
}

// evaluatePlayableWindow -- pure. positionFrame is the 0-based slider position; cutIn/cutOut are the raw
// spinBoxCutIn/spinBoxCutOut values (1-based, Out inclusive: playbackHandling stops at slider >= cutOut-1).
// The range is the one the engine plays (engineCutRangeForPlay; the collapsed-range repair is on unless
// the caller says the engine has it disabled), so the window measured here is the window that would really
// play. requestedSeconds is the window the CALLER will hold Play for; it must itself reach floorSeconds.
//
// ENFORCE-3: the window is measured in SOURCE FRAMES at the clip's NATIVE fps (ceil(20 x fps) of them), and
// the engine must be able to consume them at its ACTUAL pace (paceFps = getFramerate(), a persisted fpsOverride
// included) inside the caller's wall-clock safety budget -- else PLAY_PACE_TOO_SLOW, before Play. Advancing
// from the start frame to the Nth frame takes N-1 steps, so wallNeeded = (required - 1) / pace.
inline PlayableWindowVerdict evaluatePlayableWindow(
    int positionFrame, int cutIn, int cutOut, int totalFrames, double fps,
    double requestedSeconds, double floorSeconds = kMinPlayWindowSeconds,
    bool collapsedRangeRepairEnabled = true, double paceFps = kPaceIsNative,
    PlayPaceMode paceMode = PlayPaceMode::Gated )
{
    PlayableWindowVerdict v;
    v.requiredSeconds = std::max( floorSeconds, requestedSeconds );
    v.requestedSeconds = requestedSeconds;
    if( !( requestedSeconds + 1e-9 >= floorSeconds ) )
    {
        // The caller's own play window (its stop timer / hold) is under the floor: refused before Play,
        // whatever the clip -- 20 s of footage being available does not make a 1 s Play a 20 s Play.
        v.reason = "PLAY_DURATION_TOO_SHORT";
        v.scope = "requested";
        return v;
    }
    if( totalFrames <= 0 || !( fps > 0.0 ) )
    {
        return v; // CLIP_LENGTH_UNKNOWN, fail closed
    }

    v.clipSeconds = static_cast<double>( totalFrames ) / fps;
    const CutRange range = engineCutRangeForPlay( cutIn, cutOut, totalFrames, collapsedRangeRepairEnabled );
    v.positionFrame = clampFrameIndex( positionFrame, totalFrames );
    v.lastPlayableFrame = lastFrameIndex( range );
    v.playableFrames = v.lastPlayableFrame >= v.positionFrame
        ? v.lastPlayableFrame - v.positionFrame + 1
        : 0;
    v.playableSeconds = static_cast<double>( v.playableFrames ) / fps;
    v.requiredFrames = requiredSourceFrames( fps, v.requiredSeconds );
    // Pace: the engine must be able to consume requiredFrames inside the caller's safety budget. Computed before
    // any verdict so even a refusal reports the pace the gate measured with.
    v.paceFps = paceFps == kPaceIsNative ? fps : paceFps;
    v.wallBudgetSeconds = static_cast<double>( playSafetyMs( requestedSeconds, paceMode ) ) / 1000.0;
    v.wallNeededSeconds = v.paceFps > 0.0
        ? static_cast<double>( std::max<int64_t>( 0, v.requiredFrames - 1 ) ) / v.paceFps
        : 0.0;

    if( v.playableFrames < v.requiredFrames )
    {
        v.reason = "CLIP_TOO_SHORT";
        v.scope = v.clipSeconds + 1e-9 < v.requiredSeconds ? "clip" : "cut_range";
        return v;
    }
    if( !( v.paceFps > 0.0 ) || v.wallNeededSeconds > v.wallBudgetSeconds + 1e-9 )
    {
        v.reason = "PLAY_PACE_TOO_SLOW";
        v.scope = "pace";
        return v;
    }
    v.ok = true;
    v.reason = "";
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

// r2 (fable H7): the Look Assist settle's warm-up Play is a programmatic Play, and the process admits exactly one. The
// toggle exercise settles twice (load, then recheck after the off/on click); the second settle must NOT ask for another
// Play -- it would be REPLAY_REFUSED (exit 14) and the Auto mode could never pass. It waits for the diagnostics the
// first Play's warm-up already produced instead; if they are not valid it fails typed on the diagnostics wait.
inline bool lookAssistSettleNeedsOwnPlay( int admittedPlays ) { return admittedPlays < 1; }

// ENFORCE-4: the exit verdict of an automation Play (the MLVAPP_AUTOPLAY_SECONDS hook). It is a LATCH that fails
// CLOSED: armPending() the moment the Play is requested (before it is even admitted) leaves it failing (kFailExitCode),
// and only consumption -- resolve( Reached ) -- clears it. The process exit code is read from it (main.cpp), so an app
// that is closed (closeEvent -> quit) before the poll resolves, or that never gets to the poll, exits non-zero: a
// verdict that starts at 0 and is set by a timer is a verdict a close beats.
class AutomationVerdictLatch
{
public:
    static constexpr int kFailExitCode = 14;
    int exitCode() const { return m_exitCode; }
    bool pending() const { return m_exitCode != 0; }
    // ENFORCE-4 r2 (fable H3): STICKY. True from the moment an automation Play was requested and for the rest of the
    // process, even after the verdict resolved Reached. Automation runs never block on a prompt: closeEvent skips the
    // "save the session?" dialog while this is true (that dialog is what kept the autoplay hook from exiting offscreen).
    bool armed() const { return m_armed; }
    void armPending() { m_armed = true; m_exitCode = kFailExitCode; }
    void fail() { m_armed = true; m_exitCode = kFailExitCode; }
    void resolve( PlayStopState state ) { m_exitCode = state == PlayStopState::Reached ? 0 : kFailExitCode; }
private:
    int m_exitCode = 0;
    bool m_armed = false;
};

// ENFORCE-4 r2 (sol BLOCKER, "a receipt counts only if THIS invocation of the app wrote it, for THIS run"): a launcher
// hands the app a per-run nonce (MLVAPP_RUN_NONCE) and the app echoes it on every receipt it writes (the profile JSON's
// metadata.run_nonce, the playback_smoke.summary run_nonce=). The launcher accepts the receipt only when the echoed
// nonce is the one it generated, so a receipt left by an earlier run, or by an app that exited 0 without playing
// (--help), can never be judged. An unset or malformed nonce is written as noRunNonce(), which no launcher generates.
inline const char *noRunNonce() { return "none"; }

inline std::string sanitizeRunNonce( const char *raw )
{
    if( raw == nullptr ) return noRunNonce();
    const std::string value( raw );
    // the nonce is printed on a whitespace-delimited log line and into JSON: letters and digits only, 8..64 of them
    if( value.size() < 8 || value.size() > 64 ) return noRunNonce();
    for( char c : value )
    {
        const bool alnum = ( c >= '0' && c <= '9' ) || ( c >= 'a' && c <= 'z' ) || ( c >= 'A' && c <= 'Z' );
        if( !alnum ) return noRunNonce();
    }
    return value;
}

} // namespace playback_frame_range

#endif // PLAYBACKFRAMERANGE_H
