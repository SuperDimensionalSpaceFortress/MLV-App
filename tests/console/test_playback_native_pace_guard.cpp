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

#include "../../platform/qt/PlaybackNativePaceGuard.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <cmath>

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
    int presented = 0;
    double wallMs = 0.0;
    double timelineFps() const { return sourceAdvanced * 1000.0 / wallMs; } // simulatePlay always sets wallMs
};

// One Play of wallMs on a renderer that takes renderMs per frame. predictiveOnPresent models the CUDA texture
// route (drawFrameReady -> timerFrameEvent( true )); dropFrame=false is the every-frame mode. guarded=false is
// master (no ceiling).
PaceRun simulatePlay( double fps, double renderMs, bool predictiveOnPresent, bool dropFrame, bool guarded,
                      double wallMs = 25000.0 )
{
    NativePaceGuard guard;
    PaceRun run;
    double position = 0.0;     // m_newPosDropMode (drop frame) / slider value (normal)
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
        if( dropFrame )
        {
            const double requested = fps * timeDiff / 1000.0;
            const double granted = guarded ? guard.grant( requested, t, fps ) : requested;
            position += granted;
            run.sourceAdvanced += granted;
        }
        else if( !guarded || guard.grantWholeFrame( t, fps ) )
        {
            position += 1.0;
            run.sourceAdvanced += 1.0;
        }
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
