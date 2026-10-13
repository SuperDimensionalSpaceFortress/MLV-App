// PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1: on the CPU route with drop-frame on, the playback lookahead rendered N+1 while
// the pace clock's next drop target was often N+2 (r3, Bachelor s2: lookahead_pruned 201 of 399), and the wasted
// lookahead's recon ran on the single FIFO recon worker ahead of the frame that was shown.
//
// These tests pin the pure pieces in PlaybackPaceAwareLookahead.h:
//  - predictNextDropTarget() is the engine's own next drop target (shapedTickTimeDiffMs + NativePaceGuard::grant +
//    advanceDropFrameTick), replayed frame for frame on an injected 23.976 fps clock, and never moves the caller's guard;
//  - it falls back to N+1 (wrapped as the queue wraps) before the clock runs, and clamps to kMaxLookaheadFrames;
//  - the yield rules: target-first selection, the stale in-flight release, the lookahead-only exclusive start;
//  - depth 0 engages neither the predictor nor the yield, and the wiring keeps it that way.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackPaceAwareLookahead.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <deque>
#include <vector>

using playback_native_pace::NativePaceGuard;
using playback_pace_lookahead::ForwardWindow;
using playback_pace_lookahead::LookaheadPlan;
using playback_pace_lookahead::Prediction;
using playback_pace_lookahead::abandonInFlightLookahead;
using playback_pace_lookahead::cpuLookaheadPlan;
using playback_pace_lookahead::lookaheadDistance;
using playback_pace_lookahead::predictNextDropTarget;
using playback_pace_lookahead::predictedNextDropTarget;
using playback_pace_lookahead::reconMayStartFor;
using playback_pace_lookahead::targetFirstIndex;

namespace
{

constexpr double kFps = 24000.0 / 1001.0; // M16-1243 is 23.976
constexpr int kCutIn = 1;                 // 1-based spin-box values: frames 0..936
constexpr int kCutOut = 937;

// One engine advance as MainWindow::timerFrameEvent + playbackHandling make it in drop-frame mode.
struct Engine
{
    NativePaceGuard guard;
    double position = 0.5;
    double nowMs = 0.0;
    int cutIn = kCutIn;
    int cutOut = kCutOut;
    bool loop = false;

    Engine()
    {
        guard.grant( 0.0, nowMs, kFps ); // the first tick arms the clock
    }

    int target() const { return static_cast<int>( position ); }

    void advance( double intervalMs, bool pending )
    {
        nowMs += intervalMs;
        const int timeDiff = playback_native_pace::shapedTickTimeDiffMs( static_cast<int>( intervalMs ), kFps,
                                                                         false, true, pending );
        position = playback_frame_range::advanceDropFrameTick(
            position, guard.grant( kFps * timeDiff / 1000.0, nowMs, kFps ), cutIn, cutOut, loop ).position;
    }

    Prediction predict( double intervalMs, bool pending ) const
    {
        return predictNextDropTarget( position, guard, nowMs, intervalMs, kFps, pending, cutIn, cutOut, loop,
                                      target() );
    }
};

// Drives the engine through \a steps and checks every prediction against the advance that follows it.
std::vector<int> replayAndCheck( Engine &engine, const std::vector<double> &intervals,
                                 const std::vector<bool> &pending, int *mismatches )
{
    std::vector<int> strides;
    *mismatches = 0;
    for( std::size_t i = 0; i < intervals.size(); ++i )
    {
        const int before = engine.target();
        const Prediction prediction = engine.predict( intervals[i], pending[i] );
        engine.advance( intervals[i], pending[i] );
        const int after = engine.target();
        // A hold (no frame change) is not a target: the predictor names N+1 there, as the queue did.
        const int expected = after > before ? after : before + 1;
        if( prediction.frame != expected ) ++*mismatches;
        strides.push_back( after - before );
    }
    return strides;
}

QString readRepoText( const char *relative )
{
    QFile file( repo_file_path( QString::fromLatin1( relative ) ) );
    if( !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    return QTextStream( &file ).readAll();
}

QString bodyOf( const QString &source, const char *signature, const char *nextDefinition )
{
    const int at = source.indexOf( QString::fromLatin1( signature ) );
    if( at < 0 ) return QString();
    const int end = source.indexOf( QString::fromLatin1( nextDefinition ), at + 10 );
    return source.mid( at, end > at ? end - at : -1 );
}

// A #402-shaped guard: armed, but still waiting for this Play's first present.
struct AwaitingFirstPresentGuard
{
    bool armed() const { return true; }
    bool awaitingFirstPresent() const { return true; }
    double grant( double requestedFrames, double, double ) { return requestedFrames; }
};

struct GuardWithoutFirstPresent
{
    bool armed() const { return true; }
    double grant( double requestedFrames, double, double ) { return requestedFrames; }
};

} // namespace

// (1) busy ticks before every advance: the advance is capped to floorMs (0.983 frames), so the stride is 1.
TEST(PlaybackPaceAwareLookaheadPredict, PendingAdvancesAt70msStrideOneAndThePredictionIsNPlusOne)
{
    Engine engine;
    int mismatches = -1;
    const std::vector<int> strides = replayAndCheck( engine, std::vector<double>( 20, 70.0 ),
                                                     std::vector<bool>( 20, true ), &mismatches );
    ASSERT_EQ( mismatches, 0 );
    for( int stride : strides ) ASSERT_EQ( stride, 1 );
}

// (1) a READY hit leaves no pending flag: the advance is the whole 70 ms (1.68 frames), so it strides 1 or 2.
TEST(PlaybackPaceAwareLookaheadPredict, UncappedAdvancesAt70msStrideOneOrTwoByGrantAndThePredictionFollows)
{
    Engine engine;
    int mismatches = -1;
    const std::vector<int> strides = replayAndCheck( engine, std::vector<double>( 20, 70.0 ),
                                                     std::vector<bool>( 20, false ), &mismatches );
    ASSERT_EQ( mismatches, 0 );
    int twos = 0;
    for( int stride : strides )
    {
        ASSERT_TRUE( stride == 1 || stride == 2 );
        if( stride == 2 ) ++twos;
    }
    ASSERT_TRUE( twos >= 10 ); // 1.68 frames a tick: most ticks stride 2, which is what made N+1 a miss
}

// (1) the r3 shape: ready hit, miss, hit, miss, on uneven intervals. The predictor matches a replay of
// advanceDropFrameTick and the guard on the same inputs, frame for frame, and it is not N+1 every time.
TEST(PlaybackPaceAwareLookaheadPredict, AHitMissAlternationMatchesTheEngineReplayFrameForFrame)
{
    Engine engine;
    std::vector<double> intervals;
    std::vector<bool> pending;
    const double pattern[] = { 45.0, 70.0, 52.0, 94.0, 41.0, 88.0, 63.0, 101.0 };
    for( int lap = 0; lap < 6; ++lap )
    {
        for( std::size_t i = 0; i < sizeof( pattern ) / sizeof( pattern[0] ); ++i )
        {
            intervals.push_back( pattern[i] );
            pending.push_back( i % 2 == 1 ); // hit (no pending) then miss (pending)
        }
    }
    int mismatches = -1;
    const std::vector<int> strides = replayAndCheck( engine, intervals, pending, &mismatches );
    ASSERT_EQ( mismatches, 0 );
    int beyondNPlusOne = 0;
    for( int stride : strides ) if( stride >= 2 ) ++beyondNPlusOne;
    ASSERT_TRUE( beyondNPlusOne > 0 );
}

// (2) the predictor grants on a copy: the caller's guard keeps its credit and its arming.
TEST(PlaybackPaceAwareLookaheadPredict, ThePredictorLeavesTheCallersGuardUntouched)
{
    Engine engine;
    engine.advance( 70.0, false );
    const double creditBefore = engine.guard.creditFrames();
    const bool armedBefore = engine.guard.armed();
    const int frame = predictedNextDropTarget( engine.position, engine.guard, engine.nowMs, 300.0, kFps, false,
                                               kCutIn, kCutOut, false, engine.target() );
    ASSERT_TRUE( frame > engine.target() );
    ASSERT_EQ( engine.guard.creditFrames(), creditBefore );
    ASSERT_EQ( engine.guard.armed(), armedBefore );
    // and predicting twice gives the same answer (no hidden state)
    ASSERT_EQ( predictedNextDropTarget( engine.position, engine.guard, engine.nowMs, 300.0, kFps, false,
                                        kCutIn, kCutOut, false, engine.target() ), frame );
}

// (3) Loop wraps at the cut-out exactly as the engine does; without Loop the target clamps to the last frame.
TEST(PlaybackPaceAwareLookaheadPredict, LoopWrapsAtTheCutOutAndNoLoopClampsToTheLastFrame)
{
    Engine engine;
    engine.cutIn = 1;
    engine.cutOut = 24; // frames 0..23
    engine.loop = true;
    engine.position = 22.5;
    const Prediction wrapped = engine.predict( 70.0, false );
    engine.advance( 70.0, false );
    ASSERT_FALSE( wrapped.fallback );
    ASSERT_EQ( wrapped.frame, engine.target() );
    ASSERT_TRUE( wrapped.frame < 22 );                     // behind the target: a lap ahead
    ASSERT_TRUE( lookaheadDistance( 22, wrapped.frame, 0, 23, true ) >= 1 );

    Engine clamp;
    clamp.cutIn = 1;
    clamp.cutOut = 24;
    clamp.position = 22.5;
    const Prediction last = clamp.predict( 70.0, false );
    ASSERT_FALSE( last.fallback );
    ASSERT_EQ( last.frame, 23 );
    // at the last frame without Loop nothing is ahead: N+1 is past the cut-out, so the queue issues no lookahead
    clamp.position = 23.0;
    const Prediction past = clamp.predict( 70.0, false );
    ASSERT_TRUE( past.fallback );
    ASSERT_EQ( past.frame, 24 );
    // and with Loop the fallback wraps as queuePlaybackLookaheadRequests wraps (cutIn + (24 - cutIn) % span)
    ASSERT_EQ( playback_pace_lookahead::wrapAheadFrame( 24, 0, 23, true ), 0 );
}

// (3) a long interval never queues further ahead than kMaxLookaheadFrames.
TEST(PlaybackPaceAwareLookaheadPredict, ThePredictedDistanceIsClampedToTheMaxLookahead)
{
    Engine engine;
    engine.advance( 70.0, false );
    engine.advance( 70.0, false );
    const int target = engine.target();
    const Prediction far = engine.predict( 400.0, false ); // ~9.6 frames, the credit allows ~9.6 + carry
    ASSERT_FALSE( far.fallback );
    ASSERT_EQ( far.frame, target + playback_overlap::kMaxLookaheadFrames );
}

// (4) before the clock runs there is nothing to predict from: N+1.
TEST(PlaybackPaceAwareLookaheadPredict, AnUnarmedGuardAnUnknownIntervalOrAwaitingFirstPresentFallBackToNPlusOne)
{
    NativePaceGuard unarmed;
    const Prediction notArmed = predictNextDropTarget( 10.4, unarmed, 1000.0, 70.0, kFps, false, kCutIn, kCutOut,
                                                       false, 10 );
    ASSERT_TRUE( notArmed.fallback );
    ASSERT_EQ( notArmed.frame, 11 );

    Engine engine;
    engine.position = 10.4;
    const Prediction noInterval = engine.predict( 0.0, false );
    ASSERT_TRUE( noInterval.fallback );
    ASSERT_EQ( noInterval.frame, 11 );

    // #402: an armed guard still waiting for this Play's first present
    const Prediction awaiting = predictNextDropTarget( 10.4, AwaitingFirstPresentGuard(), 1000.0, 300.0, kFps, false,
                                                       kCutIn, kCutOut, false, 10 );
    ASSERT_TRUE( awaiting.fallback );
    ASSERT_EQ( awaiting.frame, 11 );
    // a guard without the #402 member is never "awaiting"
    ASSERT_FALSE( playback_pace_lookahead::guardAwaitingFirstPresent( GuardWithoutFirstPresent(), 0 ) );
    ASSERT_TRUE( playback_pace_lookahead::guardAwaitingFirstPresent( AwaitingFirstPresentGuard(), 0 ) );
    const Prediction without = predictNextDropTarget( 10.4, GuardWithoutFirstPresent(), 1000.0, 300.0, kFps, false,
                                                      kCutIn, kCutOut, false, 10 );
    ASSERT_FALSE( without.fallback );
}

// (5) yield (a): the shown frame is taken before any lookahead, and a lookahead waits for its own gate.
TEST(PlaybackPaceAwareLookaheadYield, TargetFirstSelectionOrder)
{
    const auto isLookahead = []( bool lookahead ) { return lookahead; };
    const std::deque<bool> mixed = { true, true, false, true };
    ASSERT_EQ( targetFirstIndex( mixed, isLookahead, true, true ), 2 );
    ASSERT_EQ( targetFirstIndex( mixed, isLookahead, true, false ), 2 );
    ASSERT_EQ( targetFirstIndex( mixed, isLookahead, false, true ), -1 ); // a waiting target is never jumped
    const std::deque<bool> onlyLookahead = { true, true };
    ASSERT_EQ( targetFirstIndex( onlyLookahead, isLookahead, true, true ), 0 );
    ASSERT_EQ( targetFirstIndex( onlyLookahead, isLookahead, true, false ), -1 );
    const std::deque<bool> fifo = { false, true, false };
    ASSERT_EQ( targetFirstIndex( fifo, isLookahead, true, true ), 0 );
    ASSERT_EQ( targetFirstIndex( std::deque<bool>(), isLookahead, true, true ), -1 );
}

// (5) yield (b): which in-flight entries are released at a hand-off.
TEST(PlaybackPaceAwareLookaheadYield, StaleInFlightReleaseDecision)
{
    ForwardWindow window;
    window.active = true;
    window.generation = 7;
    window.target = 100;
    window.cutInFrame = 0;
    window.cutOutFrame = 936;
    window.span = 2;
    ASSERT_FALSE( abandonInFlightLookahead( window, true, 7, 100 ) );  // the target itself
    ASSERT_FALSE( abandonInFlightLookahead( window, true, 7, 102 ) );  // inside [target, target + span]
    ASSERT_TRUE( abandonInFlightLookahead( window, true, 7, 99 ) );    // the target has passed it
    ASSERT_TRUE( abandonInFlightLookahead( window, true, 7, 103 ) );   // beyond the window
    ASSERT_TRUE( abandonInFlightLookahead( window, true, 6, 101 ) );   // an older generation
    ASSERT_FALSE( abandonInFlightLookahead( window, true, 8, 99 ) );   // newer than the window: not judged yet
    ASSERT_FALSE( abandonInFlightLookahead( window, false, 7, 99 ) );  // a normal request is never released
    window.active = false;
    ASSERT_FALSE( abandonInFlightLookahead( window, true, 6, 99 ) );   // yield off: nothing
    // Loop: a lap ahead is inside the window
    window.active = true;
    window.cutOutFrame = 23;
    window.target = 22;
    window.loop = true;
    ASSERT_FALSE( abandonInFlightLookahead( window, true, 7, 0 ) );
    ASSERT_TRUE( abandonInFlightLookahead( window, true, 7, 5 ) );
}

// (5) yield (c): only a lookahead recon waits for an idle render thread; the shown frame keeps today's gate.
TEST(PlaybackPaceAwareLookaheadYield, LookaheadOnlyExclusiveReconStart)
{
    // yield on, exclusive off (the default)
    ASSERT_FALSE( reconMayStartFor( true, true, false, true, false ) );  // a render is running
    ASSERT_FALSE( reconMayStartFor( true, true, false, false, true ) );  // a reconned frame waits to render
    ASSERT_TRUE( reconMayStartFor( true, true, false, false, false ) );
    ASSERT_TRUE( reconMayStartFor( false, true, false, true, true ) );   // the target never waits
    // yield off: today's gate for everything
    ASSERT_TRUE( reconMayStartFor( true, false, false, true, true ) );
    // the opt-in exclusive start still applies to every recon
    ASSERT_FALSE( reconMayStartFor( false, true, true, true, false ) );
}

// Depth 0 (the default) engages nothing: the predictor and every yield branch sit behind this plan.
TEST(PlaybackPaceAwareLookaheadPlan, DepthZeroReachesNeitherThePredictorNorTheYield)
{
    const LookaheadPlan off = cpuLookaheadPlan( 0, false, true, true, true );
    ASSERT_FALSE( off.predict );
    ASSERT_FALSE( off.yield );
    const LookaheadPlan texture = cpuLookaheadPlan( 2, true, true, true, true );
    ASSERT_FALSE( texture.predict );
    ASSERT_FALSE( texture.yield );
    const LookaheadPlan cpu = cpuLookaheadPlan( 1, false, true, true, true );
    ASSERT_TRUE( cpu.predict );
    ASSERT_TRUE( cpu.yield );
    const LookaheadPlan dropOff = cpuLookaheadPlan( 1, false, false, true, true );
    ASSERT_FALSE( dropOff.predict ); // drop-frame off presents every frame: N+1 (D1)
    ASSERT_TRUE( dropOff.yield );
    ASSERT_FALSE( cpuLookaheadPlan( 1, false, true, false, true ).predict ); // MLVAPP_CPU_LOOKAHEAD_PREDICT=0
    ASSERT_FALSE( cpuLookaheadPlan( 1, false, true, true, false ).yield );   // MLVAPP_CPU_LOOKAHEAD_YIELD=0
}

TEST(PlaybackPaceAwareLookaheadWiring, ThePredictorRunsOnlyAfterTheDepthZeroReturnAndOnlyWhenPlanned)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    ASSERT_FALSE( mw.isEmpty() );
    const QString body = bodyOf( mw, "void MainWindow::queuePlaybackLookaheadRequests(", "\nvoid MainWindow::" );
    ASSERT_FALSE( body.isEmpty() );
    const int depthReturn = body.indexOf( QStringLiteral("if( depth <= 0 ) return;") );
    const int plan = body.indexOf( QStringLiteral("cpuPlaybackLookaheadPlan()") );
    const int planned = body.indexOf( QStringLiteral("if( lookaheadPlan.predict )") );
    const int predictor = body.indexOf( QStringLiteral("playback_pace_lookahead::predictNextDropTarget(") );
    ASSERT_TRUE( depthReturn > 0 );
    ASSERT_TRUE( depthReturn < plan );
    ASSERT_TRUE( plan < planned );
    ASSERT_TRUE( planned < predictor );
    ASSERT_EQ( body.count( QStringLiteral("predictNextDropTarget(") ), 1 );
    // the predictor gets a copy of the guard; playbackHandling() is never run ahead
    ASSERT_TRUE( body.contains( QStringLiteral("m_newPosDropMode, m_playbackPaceGuard,") ) );
    ASSERT_FALSE( body.contains( QStringLiteral("playbackHandling( ") ) ); // a call; the comment names it as playbackHandling()
    // unplanned, offset 1 is N+1 and the rest follow it: today's frames
    ASSERT_TRUE( body.contains( QStringLiteral("int leadFrame = requestedFrame + 1;") ) );
    ASSERT_TRUE( body.contains( QStringLiteral("int lookaheadFrame = leadFrame + offset - 1;") ) );
    // a READY hit is the one request whose next advance is not capped
    ASSERT_TRUE( mw.contains( QStringLiteral("requestedFrame,\n                                        !playbackLookaheadReady );") ) );
    // both prunes pass the widened span and the plan's yield
    ASSERT_EQ( mw.count( QStringLiteral("playbackLookaheadPruneSpan(),") ), 2 );
    ASSERT_EQ( mw.count( QStringLiteral("cpuPlaybackLookaheadPlan().yield );") ), 2 );
    for( const char *field : { "lookahead_predicted_offset_hist=", "lookahead_predicted_hits=",
                               "lookahead_abandoned_inflight=", "MLVAPP_CPU_LOOKAHEAD_PREDICT",
                               "MLVAPP_CPU_LOOKAHEAD_YIELD" } )
    {
        ASSERT_TRUE( mw.contains( QString::fromLatin1( field ) ) );
    }
    // r2b venue ruling: the prediction stays on by default, the yield is opt-in
    ASSERT_TRUE( mw.contains( QStringLiteral("mlvappEnvSwitchOn( \"MLVAPP_CPU_LOOKAHEAD_PREDICT\" )") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("mlvappEnvSwitchOptIn( \"MLVAPP_CPU_LOOKAHEAD_YIELD\" )") ) );
}

TEST(PlaybackPaceAwareLookaheadWiring, EveryYieldBranchIsBehindTheWindowFlagThatOnlyThePruneSets)
{
    const QString rft = readRepoText( "platform/qt/RenderFrameThread.cpp" );
    ASSERT_FALSE( rft.isEmpty() );
    // the prune is the only writer of the flag (init clears the whole window)
    ASSERT_EQ( rft.count( QStringLiteral("m_lookaheadYieldWindow.active = ") ), 1 );
    ASSERT_TRUE( rft.contains( QStringLiteral("m_lookaheadYieldWindow.active = cpuRouteYield;") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("m_lookaheadYieldWindow = playback_pace_lookahead::ForwardWindow();") ) );
    // the release returns at once with the yield off
    const QString release = bodyOf( rft, "bool RenderFrameThread::abandonStaleLookaheadSlotLocked( int slotIndex )", "\n}\n" );
    ASSERT_TRUE( release.indexOf( QStringLiteral("if( !m_lookaheadYieldWindow.active ) return false;") ) > 0 );
    // each hand-off and pick tests the flag first; off, the FIFO front under today's gate
    ASSERT_TRUE( rft.contains( QStringLiteral("if( m_lookaheadYieldWindow.active && abandonStaleLookaheadSlotLocked( slotIndex ) )") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("if( !( m_lookaheadYieldWindow.active && abandonStaleLookaheadSlotLocked( slotIndex ) ) )\n            m_processReadySlots.push_back( slotIndex );") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("if( m_lookaheadYieldWindow.active ) abandonStaleLookaheadsInQueueLocked( m_processReadySlots );") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("m_lookaheadYieldWindow.active\n                 ? ( takeIndex = yieldReconTakeIndexLocked( exclusive ) ) < 0\n                 : !playback_overlap::reconMayStart( exclusive,") ) );
    const QString processPick = bodyOf( rft, "int RenderFrameThread::waitForProcessReadySlotLocked( void )", "\n}\n" );
    ASSERT_TRUE( processPick.contains( QStringLiteral("int takeIndex = 0;\n    if( m_lookaheadYieldWindow.active )") ) );
    // the window function moved, it was not copied
    ASSERT_EQ( rft.count( QStringLiteral("bool playbackFrameWithinLookaheadWindow(") ), 0 );
    ASSERT_TRUE( rft.contains( QStringLiteral("using playback_pace_lookahead::playbackFrameWithinLookaheadWindow;") ) );
}
