// PLAYBACK-CUDA-NATIVE-PACE-1: the playback timeline never advances faster than the pace fps (native, or the
// explicit fpsOverride) on any backend or scale. On Ultra-Magnus (RTX 4090, CUDA texture route, scale 1) a
// 23.976 fps clip ran at 31.6 timeline fps, because every present asked timerFrameEvent( true ) for an early
// advance and shapedTickTimeDiffMs() rounded that short tick UP to a whole frame period.
//
// The pipeline test below drives the engine-tick arithmetic MainWindow uses (shapedTickTimeDiffMs + the
// NativePaceGuard in playbackHandling) with a fake renderer and an injected clock: the 8 ms poll timer, a
// render that takes renderMs, and -- on the CUDA texture route -- the predictive early advance on every
// present. Unguarded (master) the fast renderer outruns native; guarded it never does, and a renderer slower
// than native drops or holds exactly as before.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackFrameRange.h"
#include "../../platform/qt/PlaybackNativePaceGuard.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <algorithm>
#include <cmath>
#include <vector>

using playback_native_pace::NativePaceGuard;
using playback_native_pace::kMaxCarryFrames;
using playback_native_pace::shapedTickTimeDiffMs;

namespace
{

constexpr double kNativeFps = 24000.0 / 1001.0; // 23.976
constexpr double kPollMs = 8.0;                 // mlvappPlaybackTimerIntervalMs fast default

struct PaceRun
{
    double sourceAdvanced = 0.0; // source frames the timeline advanced
    double transitions = 0.0;    // engine frame transitions: advances plus loop wraps (drop frame: the granted share)
    double maxLeadFrames = 0.0;  // the most transitions ever ran ahead of elapsed x fps
    int wraps = 0;
    int presented = 0;
    double wallMs = 0.0;
    double timelineFps() const { return sourceAdvanced * 1000.0 / wallMs; } // simulatePlay always sets wallMs
    double transitionFps() const { return transitions * 1000.0 / wallMs; }
};

// Loop on over the 1-based cut range [cutIn, cutOut] (spinBoxCutIn/spinBoxCutOut); cutOut 0 is no loop.
// payWrap=false is 8bf07ea5: advances guarded, the wrap to cut-in free.
struct LoopRange
{
    int cutIn = 0;
    int cutOut = 0;
    bool payWrap = true;
};

// One Play of wallMs on a renderer that takes renderMs per frame. predictiveOnPresent models the CUDA texture
// route (drawFrameReady -> timerFrameEvent( true )); dropFrame=false is the every-frame mode. guarded=false is
// master (no ceiling).
PaceRun simulatePlay( double fps, double renderMs, bool predictiveOnPresent, bool dropFrame, bool guarded,
                      double wallMs = 25000.0, LoopRange loop = LoopRange() )
{
    NativePaceGuard guard;
    PaceRun run;
    const bool looping = loop.cutOut > 0;
    double position = looping ? loop.cutIn - 1 : 0.0; // m_newPosDropMode (drop frame) / slider value (normal)
    long lastDrawn = -1;       // lastDrawnPlaybackPosition
    double lastTime = 0.0;     // timerFrameEvent's static lastTime
    double busyUntil = -1.0;   // the render in flight finishes here (-1: idle)
    bool pending = true;       // on_actionPlay_toggled arms m_playbackFrameAdvancePending
    double nextPoll = 0.0;

    // One timerFrameEvent( predictive ) at t (the drop-frame and normal branches of playbackHandling).
    const auto tick = [&]( double t, bool predictive )
    {
        if( busyUntil >= 0.0 && t < busyUntil && !predictive )
        {
            pending = true; // m_frameStillDrawing: record the wanted advance, lastTime untouched
            return;
        }
        const bool hadPending = pending;
        pending = false;
        const int timeDiff = shapedTickTimeDiffMs( static_cast<int>( t - lastTime ), fps, predictive, true,
                                                   hadPending );
        if( looping && std::floor( position + 1e-9 ) >= loop.cutOut - 1 ) // playbackHandling: "when on last frame"
        {
            if( !guarded || !loop.payWrap || guard.grantLoopWrap( t, fps ) )
            {
                position = loop.cutIn - 1;
                run.transitions += 1.0;
                ++run.wraps;
            }
        }
        else if( dropFrame )
        {
            const double requested = fps * timeDiff / 1000.0;
            const double granted = guarded ? guard.grant( requested, t, fps ) : requested;
            if( looping )
            {
                const playback_frame_range::DropFrameTickResult step = playback_frame_range::advanceDropFrameTick(
                    position, granted, loop.cutIn, loop.cutOut, true );
                position = step.position;
                if( step.wrapped ) ++run.wraps;
            }
            else
            {
                position += granted;
            }
            run.sourceAdvanced += granted;
            run.transitions += granted;
        }
        else if( !guarded || guard.grantWholeFrame( t, fps ) )
        {
            position += 1.0;
            run.sourceAdvanced += 1.0;
            run.transitions += 1.0;
        }
        run.maxLeadFrames = std::max( run.maxLeadFrames, run.transitions - t * fps / 1000.0 );
        const long frame = static_cast<long>( std::floor( position + 1e-9 ) );
        if( frame != lastDrawn ) // a new frame: draw it (dispatch the render)
        {
            lastDrawn = frame;
            busyUntil = t + renderMs;
        }
        lastTime = t; // drawn or idle, timerFrameEvent re-arms lastTime
    };

    while( true )
    {
        const bool renderNext = busyUntil >= 0.0 && busyUntil <= nextPoll;
        const double t = renderNext ? busyUntil : nextPoll;
        if( t > wallMs ) break;
        if( renderNext )
        {
            busyUntil = -1.0; // presented
            ++run.presented;
            if( predictiveOnPresent ) tick( t, true );
        }
        else
        {
            tick( t, false );
            nextPoll += kPollMs;
        }
    }
    run.wallMs = wallMs;
    return run;
}

// The ceiling the guard promises: kMaxCarryFrames + elapsed x pace fps.
bool atOrUnderPace( const PaceRun & run, double fps )
{
    return run.sourceAdvanced <= kMaxCarryFrames + run.wallMs * fps / 1000.0 + 1e-6;
}

} // namespace

// ---- the guard itself, on an injected clock -------------------------------------------------------------

TEST(PlaybackNativePaceGuard, FirstGrantArmsWithOneFrameAndCreditAccruesAtPace)
{
    NativePaceGuard guard;
    ASSERT_FALSE(guard.armed());
    ASSERT_TRUE(guard.grant(3.0, 1000.0, 24.0) == 1.0); // armed: one frame, not the 3 asked
    ASSERT_TRUE(guard.armed());
    ASSERT_TRUE(guard.grant(1.0, 1000.0, 24.0) == 0.0); // same instant: nothing owed
    const double half = guard.grant(1.0, 1000.0 + 500.0 / 24.0, 24.0); // half a frame period later
    ASSERT_TRUE(std::fabs(half - 0.5) < 1e-9);
}

TEST(PlaybackNativePaceGuard, ARequestWithinCreditIsGrantedUnchanged)
{
    NativePaceGuard guard;
    guard.grant(0.0, 0.0, 24.0); // arm, credit 1
    // a slow tick (100 ms = 2.4 frames owed) is a drop-frame jump: granted in full
    const double jump = guard.grant(2.4, 100.0, 24.0);
    ASSERT_TRUE(std::fabs(jump - 2.4) < 1e-9);
}

TEST(PlaybackNativePaceGuard, AStallIsNeverRepaidFasterThanPace)
{
    NativePaceGuard guard;
    guard.grant(0.0, 0.0, 24.0);
    // a 1 s stall that only took one frame (the pending-advance clamp holds) leaves at most one frame of credit
    ASSERT_TRUE(std::fabs(guard.grant(1.0, 1000.0, 24.0) - 1.0) < 1e-9);
    ASSERT_TRUE(guard.creditFrames() <= kMaxCarryFrames + 1e-12);
    // so the very next instant can take at most that one carried frame, not the 23 the stall "owed"
    ASSERT_TRUE(guard.grant(30.0, 1000.0, 24.0) <= kMaxCarryFrames + 1e-12);
}

TEST(PlaybackNativePaceGuard, WholeFrameModeHoldsUntilAFrameIsOwed)
{
    NativePaceGuard guard;
    ASSERT_TRUE(guard.grantWholeFrame(0.0, 24.0));   // first tick advances at once, as before
    ASSERT_FALSE(guard.grantWholeFrame(8.0, 24.0));  // 8 ms poll: 0.19 frame owed, hold
    ASSERT_FALSE(guard.grantWholeFrame(40.0, 24.0)); // 0.96 frame owed, hold
    ASSERT_TRUE(guard.grantWholeFrame(42.0, 24.0));  // 1.008 frame owed
}

TEST(PlaybackNativePaceGuard, ResetRearmsAndAnUnknownPaceIsLeftAlone)
{
    NativePaceGuard guard;
    guard.grant(0.0, 0.0, 24.0);
    guard.grant(1.0, 0.0, 24.0);
    guard.reset();
    ASSERT_FALSE(guard.armed());
    ASSERT_TRUE(guard.grant(5.0, 9999.0, 24.0) == 1.0); // re-armed with one frame
    NativePaceGuard unknown;
    ASSERT_TRUE(unknown.grant(5.0, 0.0, 0.0) == 5.0);
    ASSERT_TRUE(unknown.grantWholeFrame(0.0, -1.0));
}

TEST(PlaybackNativePaceGuard, TickShapingIsTimerFrameEventsArithmetic)
{
    // 23.976 fps: frame period 41.708 ms, floor 41, ceil 42
    ASSERT_EQ(42, shapedTickTimeDiffMs(30, kNativeFps, true, true, false));  // early predictive tick rounded UP
    ASSERT_EQ(30, shapedTickTimeDiffMs(30, kNativeFps, false, true, false)); // the poll keeps real time
    ASSERT_EQ(30, shapedTickTimeDiffMs(30, kNativeFps, true, false, false)); // not playing: real time
    ASSERT_EQ(41, shapedTickTimeDiffMs(90, kNativeFps, false, true, true));  // stall after a pending advance
    ASSERT_EQ(42, shapedTickTimeDiffMs(90, kNativeFps, true, true, true));
    ASSERT_EQ(90, shapedTickTimeDiffMs(90, kNativeFps, false, true, false)); // drop-frame catch-up jump
}

// ---- the pipeline: fake renderer, injected clock ----------------------------------------------------------

TEST(PlaybackNativePace, MasterOutrunsNativeWithAFastCudaRenderer)
{
    // The bug, reproduced: unguarded, a 30 ms CUDA render advances one frame per present (~33 fps).
    const PaceRun master = simulatePlay(kNativeFps, 30.0, true, true, false);
    ASSERT_TRUE(master.timelineFps() > kNativeFps * 1.2);
    ASSERT_FALSE(atOrUnderPace(master, kNativeFps));
}

TEST(PlaybackNativePace, AFastCudaRendererNeverOutrunsNative)
{
    for (double renderMs : {2.0, 10.0, 20.0, 30.0, 35.0, 40.0})
    {
        const PaceRun run = simulatePlay(kNativeFps, renderMs, true, true, true);
        ASSERT_TRUE(atOrUnderPace(run, kNativeFps));
        ASSERT_TRUE(run.timelineFps() <= kNativeFps * 1.02);
        // and it is not slowed: a renderer faster than native still holds native pace
        ASSERT_TRUE(run.timelineFps() >= kNativeFps * 0.97);
    }
}

TEST(PlaybackNativePace, TheCpuRouteAndEveryFrameModeNeverOutrunNative)
{
    for (double renderMs : {2.0, 10.0, 30.0})
    {
        ASSERT_TRUE(atOrUnderPace(simulatePlay(kNativeFps, renderMs, false, true, true), kNativeFps));
        const PaceRun everyFrame = simulatePlay(kNativeFps, renderMs, true, false, true);
        ASSERT_TRUE(atOrUnderPace(everyFrame, kNativeFps));
        ASSERT_TRUE(everyFrame.timelineFps() >= kNativeFps * 0.97);
    }
    // master's every-frame mode with the 8 ms poll ran at the poll rate on a fast renderer
    ASSERT_TRUE(simulatePlay(kNativeFps, 2.0, false, false, false).timelineFps() > kNativeFps * 2.0);
}

TEST(PlaybackNativePace, AnExplicitOverridePaceIsTheCeiling)
{
    const PaceRun run = simulatePlay(50.0, 5.0, true, true, true);
    ASSERT_TRUE(atOrUnderPace(run, 50.0));
    ASSERT_TRUE(run.timelineFps() >= 50.0 * 0.97);
}

TEST(PlaybackNativePace, ASlowRendererDropsOrHoldsExactlyAsBefore)
{
    // slower than native: the guard never binds, so the timeline is master's to the frame
    for (double renderMs : {45.0, 60.0, 90.0, 250.0})
    {
        for (bool predictive : {true, false})
        {
            const PaceRun master = simulatePlay(kNativeFps, renderMs, predictive, true, false);
            const PaceRun guarded = simulatePlay(kNativeFps, renderMs, predictive, true, true);
            ASSERT_TRUE(std::fabs(master.sourceAdvanced - guarded.sourceAdvanced) < 1e-6);
            ASSERT_EQ(master.presented, guarded.presented);
            ASSERT_TRUE(guarded.timelineFps() <= kNativeFps);
        }
    }
}

// ---- the loop boundary: the wrap from cut-out back to cut-in is a frame transition too ---------------------

namespace
{
constexpr double kHourMs = 3600.0 * 1000.0;
}

TEST(PlaybackNativePaceLoop, AnUnpaidWrapOutrunsNativeOnAShortLoop)
{
    // sol r1, reproduced: normal mode, Loop on, cut 1..24, 32 ms render. 8bf07ea5 paced the 23 advances of a lap
    // but not the wrap, so 24 transitions took 23 frame periods: 3003 in 120 s, 25.025 fps.
    const PaceRun unpaid = simulatePlay(kNativeFps, 32.0, false, false, true, 120000.0, LoopRange{1, 24, false});
    ASSERT_TRUE(unpaid.wraps > 100);
    ASSERT_TRUE(unpaid.transitionFps() > kNativeFps * 1.03);
    ASSERT_TRUE(unpaid.transitions > 1.0 + 120000.0 * kNativeFps / 1000.0);
}

TEST(PlaybackNativePaceLoop, AShortLoopNeverOutrunsNativeForAnHourInEveryMode)
{
    // a fast renderer, an hour of simulated wall time, normal and drop-frame, predictive and not: the
    // transitions (wraps included) never run more than the carried frame ahead of elapsed x pace at ANY tick,
    // and the loop is not slowed below native either -- so there is no drift in either direction.
    for (bool dropFrame : {false, true})
    {
        for (bool predictive : {true, false})
        {
            for (int cutOut : {2, 24})
            {
                const PaceRun run = simulatePlay(kNativeFps, 2.0, predictive, dropFrame, true, kHourMs,
                                                 LoopRange{1, cutOut, true});
                ASSERT_TRUE(run.wraps > 1000);
                ASSERT_TRUE(run.maxLeadFrames <= kMaxCarryFrames + 1e-6);
                ASSERT_TRUE(run.transitions <= kMaxCarryFrames + kHourMs * kNativeFps / 1000.0 + 1e-6);
                ASSERT_TRUE(run.transitionFps() >= kNativeFps * 0.97);
            }
        }
    }
    // and the sol r1 case itself, now paid
    const PaceRun paid = simulatePlay(kNativeFps, 32.0, false, false, true, kHourMs, LoopRange{1, 24, true});
    ASSERT_TRUE(paid.maxLeadFrames <= kMaxCarryFrames + 1e-6);
    ASSERT_TRUE(paid.transitionFps() >= kNativeFps * 0.97);
}

TEST(PlaybackNativePaceLoop, ASlowRendererLoopsExactlyAsBefore)
{
    // slower than native, the wrap's frame of credit is always there: master's loop to the frame
    for (double renderMs : {45.0, 60.0, 90.0, 250.0})
    {
        for (bool dropFrame : {false, true})
        {
            for (bool predictive : {true, false})
            {
                const LoopRange range{1, 24, true};
                const PaceRun master = simulatePlay(kNativeFps, renderMs, predictive, dropFrame, false, 120000.0, range);
                const PaceRun guarded = simulatePlay(kNativeFps, renderMs, predictive, dropFrame, true, 120000.0, range);
                ASSERT_TRUE(std::fabs(master.transitions - guarded.transitions) < 1e-6);
                ASSERT_EQ(master.wraps, guarded.wraps);
                ASSERT_EQ(master.presented, guarded.presented);
                ASSERT_TRUE(guarded.wraps > 0);
            }
        }
    }
}

// ---- throughput, measured the way the GUI smoke measures it (PLAYBACK-PACE-GUARD-THROUGHPUT-1) ---------------
//
// UM r5 (dd15567b, CUDA, 23.976 clip) read timeline 20.985 / presented 17.516 fps; r2 (97e05ff3, before the
// guard) read 31.562 on the same venue. The guard was not what slowed it, but r5's timeline was NOT native after
// the first present either. r5's log: Play 14:50:00.905, first texture 14:50:04.562 (r2: 0.457 s), stop 14:50:29.450
// at slider 599. The whole-run figure counts the 3.75 s wait for the first frame; the first present's early advance
// then repaid that wait in one jump (>= 64 frames), so 599 / (elapsed - first present) = 24.16 is NOT a paced rate.
// The contact-sheet sidecars give it: from display frame 125 on, 21.3 fps, 11 % under native. Every frame cost more
// than in r2: render_work 16.38 ms (r2 8.35), llrawproc 21.41 (13.75), UI signal latency 13.54 (7.54), avg present
// interval 49.68 ms > the 41.71 ms period, with host non-subject CPU at 100 % (r2 84 %). A cycle that slow never
// meets the guard: it holds a renderer only while it is faster than native. Drop-frame mode did not hold native
// there either: it tied the timeline to ~1 frame per present (PLAYBACK-DROPFRAME-SLOW-RENDER-TIMELINE-1).

namespace
{

struct SmokeRun
{
    double elapsedMs = 0.0;      // Play to stop (playback_smoke.summary elapsed_ms)
    double firstPresentMs = -1.0; // first_present_ms
    int presented = 0;
    long timelineDelta = 0;      // the slider's advance (timeline_delta)
    long timelineAtFirstPresent = 0; // the slider's advance when the first present is recorded (catch-up included)
    double timelineFps() const { return timelineDelta * 1000.0 / elapsedMs; }
    double timelineFpsAfterFirstPresent() const
    {
        return playback_native_pace::fpsAfterFirstPresent(
            playback_native_pace::timelineFramesAfterFirstPresent( static_cast<int>( timelineDelta ),
                                                                   static_cast<int>( timelineAtFirstPresent ) ),
            elapsedMs, firstPresentMs, presented );
    }
    double presentedFpsAfterFirstPresent() const
    {
        return playback_native_pace::fpsAfterFirstPresent( presented - 1, elapsedMs, firstPresentMs, presented );
    }
};

// One measured Play on the CUDA texture route (predictive early advance on every present). Play dispatches frame
// 0's render, which takes firstRenderMs (r5: the CUDA backend's first use); every later render takes renderMs x
// renderScale[i % n] from dispatch to frame-ready, and drawFrameReady runs uiMs after that (a queued signal:
// m_frameStillDrawing stays set until it runs, so a poll in between only records a pending advance). Play stops
// when the slider reaches requiredFrames - 1, as the smoke's source-frame oracle stops it.
SmokeRun simulateSmokePlay( double renderMs, double uiMs, double firstRenderMs, bool dropFrame, bool guarded,
                            const std::vector<double> & renderScale = { 1.0 }, int requiredFrames = 600 )
{
    NativePaceGuard guard;
    SmokeRun run;
    double position = 0.0;
    long lastDrawn = 0;
    double lastTime = 0.0;
    bool drawing = true;               // frame 0's render, dispatched by Play
    double presentAt = firstRenderMs + uiMs;
    bool pending = true;
    double nextPoll = 0.0;
    size_t renders = 0;

    const auto tick = [&]( double t, bool predictive )
    {
        if( drawing && !predictive )
        {
            pending = true;
            return;
        }
        const bool hadPending = pending;
        pending = false;
        const int timeDiff = shapedTickTimeDiffMs( static_cast<int>( t - lastTime ), kNativeFps, predictive, true,
                                                   hadPending );
        if( dropFrame )
        {
            const double requested = kNativeFps * timeDiff / 1000.0;
            position += guarded ? guard.grant( requested, t, kNativeFps ) : requested;
        }
        else if( !guarded || guard.grantWholeFrame( t, kNativeFps ) )
        {
            position += 1.0;
        }
        const long frame = static_cast<long>( std::floor( position + 1e-9 ) );
        if( frame != lastDrawn )
        {
            lastDrawn = frame;
            drawing = true;
            presentAt = t + renderMs * renderScale[renders++ % renderScale.size()] + uiMs;
        }
        lastTime = t;
    };

    while( lastDrawn < requiredFrames - 1 )
    {
        const bool presentNext = drawing && presentAt <= nextPoll;
        const double t = presentNext ? presentAt : nextPoll;
        if( presentNext )
        {
            drawing = false;
            const bool first = ++run.presented == 1;
            if( first ) run.firstPresentMs = t;
            tick( t, true );
            // the app runs the first present's early advance BEFORE it records the present, so the slider is
            // sampled after the tick, as MainWindow does
            if( first ) run.timelineAtFirstPresent = lastDrawn;
        }
        else
        {
            tick( t, false );
            nextPoll += kPollMs;
        }
        run.elapsedMs = t;
    }
    run.timelineDelta = lastDrawn;
    return run;
}

constexpr double kGateCeilingFps = kNativeFps * 1.02; // the venue gate: timeline <= native x 1.02 (24.455)

} // namespace

TEST(PlaybackNativePaceThroughput, AFastRendererReachesNativeAndPresentsEveryFrame)
{
    // render + UI latency under the 41.7 ms period: the guard paces the timeline to native, and every frame the
    // timeline reaches is presented (no drop-frame skipping), in both modes
    const double cases[][2] = { { 10.0, 0.0 }, { 10.0, 7.5 }, { 25.0, 0.0 }, { 25.06, 7.54 }, // r2's render + UI
                                { 32.0, 0.0 }, { 32.0, 7.5 }, { 36.0, 0.0 } };
    for( bool dropFrame : { true, false } )
    {
        for( const auto & c : cases )
        {
            const SmokeRun run = simulateSmokePlay( c[0], c[1], 500.0, dropFrame, true );
            ASSERT_TRUE( run.timelineFpsAfterFirstPresent() >= 23.5 );
            ASSERT_TRUE( run.timelineFpsAfterFirstPresent() <= kGateCeilingFps );
            ASSERT_TRUE( run.presentedFpsAfterFirstPresent() >= 23.5 );
            ASSERT_TRUE( run.timelineDelta - run.presented <= 1 );
            // master, for contrast, ran the same renderer at its own rate
            ASSERT_TRUE( simulateSmokePlay( c[0], c[1], 500.0, dropFrame, false ).timelineFpsAfterFirstPresent()
                         > kGateCeilingFps );
        }
    }
}

TEST(PlaybackNativePaceThroughput, AJitteryFastRendererStillReachesNative)
{
    // a mean cycle under the period with a stall every fifth frame (25 x {0.6, 0.6, 0.6, 0.6, 2.6} + 7.5 ms: 22.5
    // to 72.5 ms per frame): the carried frame repays a one-frame stall, so the timeline still reaches native
    for( bool dropFrame : { true, false } )
    {
        const SmokeRun run = simulateSmokePlay( 25.0, 7.5, 500.0, dropFrame, true, { 0.6, 0.6, 0.6, 0.6, 2.6 } );
        ASSERT_TRUE( run.timelineFpsAfterFirstPresent() >= 23.5 );
        ASSERT_TRUE( run.timelineFpsAfterFirstPresent() <= kGateCeilingFps );
    }
}

TEST(PlaybackNativePaceThroughput, AFirstFrameWaitLowersTheWholeRunRateNotThePacedRate)
{
    // the hub's model (a 32 ms renderer, the 8 ms poll) on dd15567b's guard reaches native once frames flow; given
    // r5's 3.75 s first frame, the smoke's whole-run timeline_fps reads ~21 all the same. (This model cuts the first
    // present's tick to one frame, so it has no catch-up: it says nothing about r5's own timeline.)
    const SmokeRun run = simulateSmokePlay( 32.0, 7.5, 3745.0, true, true );
    ASSERT_TRUE( run.timelineFps() > 20.5 && run.timelineFps() < 21.3 );
    ASSERT_TRUE( run.timelineFpsAfterFirstPresent() >= 23.5 );
    ASSERT_TRUE( run.timelineAtFirstPresent <= 2 );
    ASSERT_TRUE( std::fabs( 599.0 * 1000.0 / 28544.867 - 20.985 ) < 0.001 ); // r5's whole-run timeline_fps
}

TEST(PlaybackNativePaceThroughput, TheR5TimelineWasBelowNativeAfterTheFirstPresent)
{
    // UM r5 (de9842cc), contact-sheet sidecars (elapsed_ms, display_frame): (3670, 0) (6256, 125) (10423, 223)
    // (14637, 310) (18760, 393) (22936, 481); the Play stopped at slider 599, summary elapsed_ms 28544.867,
    // first_present_ms 3752.904. 125 frames in the 50 presents after the first one is the catch-up for the wait;
    // from there the timeline ran at 1.06 frames per present.
    const double sidecarFps = 1000.0 * ( 481 - 125 ) / ( 22936 - 6256 ); // one clock, no offsets: 21.343
    ASSERT_TRUE( sidecarFps > 21.3 && sidecarFps < 21.4 );
    const double toStopFps = 1000.0 * ( 599 - 125 ) / ( 28544.867 - 6256 ); // the stop is on the summary clock: 21.27
    ASSERT_TRUE( toStopFps > 21.2 && toStopFps < 21.4 );
    ASSERT_TRUE( sidecarFps < kNativeFps * 0.9 && toStopFps < kNativeFps * 0.9 ); // 11 % under native, not at it

    // round 1 divided the WHOLE run's 599 frames by the post-first-present time: 24.16 reads native, but it counts
    // the catch-up. That is the number this test refuses to bless again.
    const double r5Elapsed = 28544.867, r5FirstPresent = 3752.904;
    const double wholeRunFramesOverPacedTime = playback_native_pace::fpsAfterFirstPresent( 599.0, r5Elapsed, r5FirstPresent, 500 );
    ASSERT_TRUE( wholeRunFramesOverPacedTime > 24.15 && wholeRunFramesOverPacedTime < 24.17 );
    // the receipt did not log the slider at the first present; any catch-up of at least the 64 frames the sidecars
    // bound it from below puts the paced rate where the sidecars put it, far under the 23.5 a native run clears
    for( int catchUp : { 64, 70, 76 } )
    {
        const double paced = playback_native_pace::fpsAfterFirstPresent(
            playback_native_pace::timelineFramesAfterFirstPresent( 599, catchUp ), r5Elapsed, r5FirstPresent, 500 );
        ASSERT_TRUE( paced > 21.0 && paced < 21.8 );
    }
    // the presented rate after the first present: 499 presents in 24.79 s = 20.1 fps, 1000 / 49.68
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 499.0, r5Elapsed, r5FirstPresent, 500 ) < 20.2 );
}

TEST(PlaybackNativePaceThroughput, TimelineFramesAfterFirstPresentExcludeTheCatchUp)
{
    ASSERT_EQ( 526, playback_native_pace::timelineFramesAfterFirstPresent( 599, 73 ) );
    ASSERT_EQ( 598, playback_native_pace::timelineFramesAfterFirstPresent( 599, 1 ) ); // a normal first step
    ASSERT_EQ( 599, playback_native_pace::timelineFramesAfterFirstPresent( 599, 0 ) );
    ASSERT_EQ( 0, playback_native_pace::timelineFramesAfterFirstPresent( 599, 599 ) );
    ASSERT_EQ( 0, playback_native_pace::timelineFramesAfterFirstPresent( 10, 599 ) ); // never negative
    // a 3 s wait repaid in one jump: the paced rate is the rate after it, not the run's frames over the paced time
    const double withJump = playback_native_pace::fpsAfterFirstPresent(
        playback_native_pace::timelineFramesAfterFirstPresent( 600, 72 ), 25000.0, 3000.0, 400 );
    ASSERT_TRUE( std::fabs( withJump - 24.0 ) < 1e-9 ); // 528 frames in 22 s
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 600.0, 25000.0, 3000.0, 400 ) > 27.0 ); // the old, inflated read
}

TEST(PlaybackNativePaceThroughput, ASlowRendererIsTheSameWithOrWithoutTheGuard)
{
    // r5's cycle (render_total 41.37 + UI 13.54 ms) and slower: the guard never binds, master to the frame
    const double cases[][2] = { { 41.37, 13.54 }, { 36.2, 13.54 }, { 45.0, 7.5 }, { 60.0, 0.0 } };
    for( bool dropFrame : { true, false } )
    {
        for( const auto & c : cases )
        {
            const SmokeRun guarded = simulateSmokePlay( c[0], c[1], 3745.0, dropFrame, true );
            const SmokeRun master = simulateSmokePlay( c[0], c[1], 3745.0, dropFrame, false );
            ASSERT_EQ( master.presented, guarded.presented );
            ASSERT_EQ( master.timelineDelta, guarded.timelineDelta );
            ASSERT_TRUE( std::fabs( master.elapsedMs - guarded.elapsedMs ) < 1e-6 );
            ASSERT_TRUE( guarded.presentedFpsAfterFirstPresent() < kNativeFps );
        }
    }
}

TEST(PlaybackNativePaceThroughput, FpsAfterFirstPresentNeedsAPacedInterval)
{
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 240.0, 11000.0, 1000.0, 241 ) == 24.0 );
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 240.0, 11000.0, 1000.0, 0 ) == 0.0 );  // nothing presented
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 240.0, 10000.0, 0.0, 1 ) == 24.0 ); // presented at 0 ms still counts
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 240.0, 1000.0, 1000.0, 1 ) == 0.0 );  // no interval after it
    ASSERT_TRUE( playback_native_pace::fpsAfterFirstPresent( 240.0, 900.0, 1000.0, 1 ) == 0.0 );
}

// ---- wiring: MainWindow.cpp needs a full GUI build, so its source is read as text --------------------------

namespace
{

QString mainWindowSource()
{
    const QString path = repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString bodyBetween(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackNativePaceWiring, EveryEngineAdvanceGoesThroughThePaceGuard)
{
    const QString source = mainWindowSource();
    const QString handling = bodyBetween(source, QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"),
                                         QStringLiteral("void MainWindow::showPerformanceProfilingDialog"));
    ASSERT_FALSE(handling.isEmpty());
    // every-frame mode holds unless a whole frame is owed, BEFORE it moves the slider
    const int whole = handling.indexOf(QStringLiteral(
        "if( !m_playbackPaceGuard.grantWholeFrame( paceNowMs, getFramerate() ) ) return;"));
    const int normalAdvance = handling.indexOf(QStringLiteral(
        "ui->horizontalSliderPosition->setValue( ui->horizontalSliderPosition->value() + 1 );"));
    ASSERT_TRUE(whole >= 0);
    ASSERT_TRUE(normalAdvance > whole);
    // the loop wrap to cut-in (either mode) spends a whole frame BEFORE it is counted or moves the slider
    const int wrapGate = handling.indexOf(QStringLiteral(
        "if( !m_playbackPaceGuard.grantLoopWrap( paceNowMs, getFramerate() ) ) return;"));
    const int wrapCounted = handling.indexOf(QStringLiteral("m_playbackWrapRecorder.noteEngineWrap();"));
    const int wrapMove = handling.indexOf(QStringLiteral("ui->horizontalSliderPosition->setValue( cutInFrame );"));
    const int loopBranch = handling.indexOf(QStringLiteral("if( ui->actionLoop->isChecked() )"));
    ASSERT_TRUE(loopBranch >= 0);
    ASSERT_TRUE(wrapGate > loopBranch);
    ASSERT_TRUE(wrapCounted > wrapGate);
    ASSERT_TRUE(wrapMove > wrapGate);
    ASSERT_TRUE(handling.indexOf(QStringLiteral("const double paceNowMs = mlv_stage_timing_now() * 1000.0;")) < wrapGate);
    // drop-frame mode advances by the GRANTED share, never the raw fps x timeDiff
    ASSERT_TRUE(handling.contains(QStringLiteral(
        "m_playbackPaceGuard.grant( getFramerate() * (double)timeDiff / 1000.0, paceNowMs, getFramerate() ),")));
    ASSERT_FALSE(handling.contains(QStringLiteral("m_newPosDropMode, getFramerate() * (double)timeDiff / 1000.0,")));
    ASSERT_TRUE(handling.contains(QStringLiteral("const double paceNowMs = mlv_stage_timing_now() * 1000.0;")));
    ASSERT_TRUE(handling.contains(QStringLiteral("m_playbackPaceGuard.reset();"))); // paused: re-arm at next Play

    const QString timer = bodyBetween(source, QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"),
                                      QStringLiteral("void MainWindow::timerEvent(QTimerEvent *t)"));
    ASSERT_FALSE(timer.isEmpty());
    ASSERT_TRUE(timer.contains(QStringLiteral("playback_native_pace::shapedTickTimeDiffMs( timeDiff, getFramerate(), predictivePlaybackAdvance,")));
    ASSERT_FALSE(timer.contains(QStringLiteral("timeDiff = targetFrameMsCeil;"))); // the shaping lives in one place

    const QString toggled = bodyBetween(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
                                        QStringLiteral("MainWindow::checkPlayableWindow("));
    ASSERT_FALSE(toggled.isEmpty());
    ASSERT_TRUE(toggled.contains(QStringLiteral("m_playbackPaceGuard.reset();")));
}

TEST(PlaybackNativePaceWiring, TheSmokeReportsThePacedRatesBesideTheWholeRun)
{
    const QString source = mainWindowSource();
    const int summary = source.indexOf(QStringLiteral("\"playback_smoke.summary session=%1 reason=%2 elapsed_ms=%3 \""));
    const int pace = source.indexOf(QStringLiteral(
        "\"playback_smoke.pace_summary session=%1 first_present_ms=%2 paced_elapsed_ms=%3 \""));
    const int gpu = source.indexOf(QStringLiteral("\"playback_smoke.gpu_summary session=%1 cpu_frames=%2 \""));
    ASSERT_TRUE(summary >= 0);
    ASSERT_TRUE(pace > summary);
    ASSERT_TRUE(gpu > pace);
    const QString line = source.mid(pace, gpu - pace);
    ASSERT_TRUE(line.contains(QStringLiteral(
        "\"timeline_fps_after_first_present=%4 presented_fps_after_first_present=%5 pace_fps=%6 \"\n"
        "               \"first_present_catchup_frames=%7\" )")));
    ASSERT_TRUE(line.contains(QStringLiteral(".arg( m_playbackSmokePresentedFrames > 0 ? elapsedMs - m_playbackSmokeFirstPresentMs : 0.0, 0, 'f', 3 )")));
    // BOTH rates take the elapsed time, the first-present time and the presented count, in that order, each from
    // its own call: a call that swaps or drops the first-present time reads the whole run
    ASSERT_TRUE(line.contains(QStringLiteral(
        ".arg( playback_native_pace::fpsAfterFirstPresent(\n"
        "                         playback_native_pace::timelineFramesAfterFirstPresent( timelineDeltaAbs, m_playbackSmokeFirstPresentTimelineDeltaAbs ),\n"
        "                         elapsedMs, m_playbackSmokeFirstPresentMs, m_playbackSmokePresentedFrames ), 0, 'f', 3 )")));
    ASSERT_TRUE(line.contains(QStringLiteral(
        ".arg( playback_native_pace::fpsAfterFirstPresent(\n"
        "                         qMax( 0, m_playbackSmokePresentedFrames - 1 ),\n"
        "                         elapsedMs, m_playbackSmokeFirstPresentMs, m_playbackSmokePresentedFrames ), 0, 'f', 3 )")));
    ASSERT_EQ(2, line.count(QStringLiteral("elapsedMs, m_playbackSmokeFirstPresentMs, m_playbackSmokePresentedFrames ), 0, 'f', 3 )")));
    ASSERT_EQ(2, line.count(QStringLiteral("playback_native_pace::fpsAfterFirstPresent(")));
    ASSERT_TRUE(line.contains(QStringLiteral(
        ".arg( m_playPaceFps, 0, 'f', 3 )\n"
        "               .arg( m_playbackSmokeFirstPresentTimelineDeltaAbs );")));
}

TEST(PlaybackNativePaceWiring, TheFirstPresentsCatchUpIsSampledAfterItsAdvanceAndNotPaced)
{
    const QString source = mainWindowSource();
    // sampled where the first present is recorded: drawFrameReady runs the first present's early advance before
    // notePlaybackSmokePresentedFrame, so the catch-up is already in the slider
    const int firstPresent = source.indexOf(QStringLiteral("        m_playbackSmokeFirstPresentMs = elapsedMs;\n"));
    ASSERT_TRUE(firstPresent >= 0);
    const int sample = source.indexOf(QStringLiteral(
        "        m_playbackSmokeFirstPresentTimelineDeltaAbs =\n"
        "            qAbs( ui->horizontalSliderPosition->value() - m_playbackSmokeStartPosition );"), firstPresent);
    ASSERT_TRUE(sample > firstPresent);
    ASSERT_TRUE(sample - firstPresent < 1200);
    ASSERT_TRUE(source.mid(firstPresent - 200, 200).contains(QStringLiteral("else")));
    // forgotten at every Play start
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_playbackSmokeFirstPresentMs = 0.0;\n"
        "    m_playbackSmokeFirstPresentTimelineDeltaAbs = 0;\n")));
}
