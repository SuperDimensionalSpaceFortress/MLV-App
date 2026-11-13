// PLAYBACK-DECODE-RENDER-OVERLAP-1: on the CUDA texture route the decode + CPU dual-ISO of frame N+1 must run while
// frame N renders and presents. Before this card the stages ran in series with one frame in flight: the GUI only
// asked for N+1 once N was rendered, and the existing playback lookahead (MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES)
// could not help, because after every present the GUI recomputed m_frameStillDrawing from
// RenderFrameThread::isIdle(), which counts the speculative lookahead work as busy and so held the timer.
//
// The pipeline test below drives the real pieces MainWindow uses -- NativePaceGuard (the #261 ceiling),
// playback_overlap::effectiveLookaheadFrames, playback_overlap::blocksPlaybackAdvance and the OverlapMeter -- through
// a fake render pipeline on an injected clock: one upstream worker (decode + dual-ISO), one render thread, the GUI
// hop, and the 8 ms poll timer. Bachelor's measured stage costs (30 / 11 / 6 ms) give ~21 fps in series; with two
// frames in flight the timeline holds native pace, frames still present one by one in order, and the meter reports
// the overlap. With the old gate (isIdle: every in-flight frame is busy) the lookahead buys nothing.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDecodeRenderOverlap.h"
#include "../../platform/qt/PlaybackNativePaceGuard.h"
#include "../../platform/qt/PlaybackLookaheadCpuRoute.h"
#include "../../platform/qt/GuiPresentGapIdle.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <cstdio>
#include <deque>
#include <limits>
#include <map>
#include <vector>

using playback_native_pace::NativePaceGuard;
using playback_overlap::OverlapMeter;
using playback_overlap::OverlapSnapshot;
using playback_overlap::WorkItem;
using playback_overlap::blocksPlaybackAdvance;
using playback_overlap::effectiveLookaheadFrames;

namespace
{

constexpr double kNativeFps = 24000.0 / 1001.0; // M16-1243 is 23.976
constexpr double kPollMs = 8.0;
constexpr double kStepMs = 0.25;

enum class Gate
{
    IsIdle,             // before: any in-flight frame holds the timer
    ExceptLookahead     // after: only the target's own work and non-lookahead work hold it
};

struct SimFrame
{
    bool lookahead = false;
    int stage = 0;          // 0 queued upstream, 1 upstream, 2 queued render, 3 render, 4 ready, 5 presented
    double doneMs = 0.0;
};

struct PipelineRun
{
    int presented = 0;
    bool inOrder = true;    // every present is exactly the previous one + 1
    bool presentedAny = false;
    double firstPresentMs = 0.0;
    double lastPresentMs = 0.0;
    int maxInFlight = 0;
    OverlapSnapshot overlap;
    double presentedFpsAfterFirst() const
    {
        return ( presented > 1 && lastPresentMs > firstPresentMs )
            ? ( presented - 1 ) * 1000.0 / ( lastPresentMs - firstPresentMs )
            : 0.0;
    }
};

// Every-frame mode (grantWholeFrame), Loop off, wallMs of playback. While the upstream and the render run at once each
// progresses at 1/contention of its speed (both are OpenMP teams over every core); exclusiveRecon is reconMayStart().
PipelineRun simulatePipeline( int lookaheadDepth, Gate gate, double upstreamMs, double renderMs, double hopMs,
                              double contention = 1.0, bool exclusiveRecon = false, double wallMs = 25000.0 )
{
    PipelineRun run;
    NativePaceGuard guard;
    OverlapMeter meter;
    meter.reset( 0.0 );

    std::map<int, SimFrame> frames;     // frame number -> state (presented frames are erased)
    std::deque<int> upstreamQueue;
    std::deque<int> renderQueue;
    int upstreamBusy = -1;
    int renderBusy = -1;
    double upstreamLeft = 0.0;          // uncontended work left, ms
    double renderLeft = 0.0;

    int target = -1;                    // the frame the GUI asked for
    int lastPresented = -1;
    bool stillDrawing = false;
    bool presentScheduled = false;
    double presentAt = 0.0;
    double nextPollMs = 0.0;

    const auto workItems = [&]()
    {
        std::vector<WorkItem> items;
        for( const auto &entry : frames )
        {
            if( entry.second.stage >= 4 ) continue; // Ready frames are not in flight (as in isIdle())
            WorkItem item;
            item.playbackLookahead = entry.second.lookahead;
            item.generation = 1;
            item.frameNumber = entry.first;
            items.push_back( item );
        }
        return items;
    };
    const auto busyForAdvance = [&]()
    {
        const std::vector<WorkItem> items = workItems();
        if( gate == Gate::IsIdle ) return !items.empty();
        for( const WorkItem &item : items )
        {
            if( blocksPlaybackAdvance( item, target, 1 ) ) return true;
        }
        return false;
    };
    const auto request = [&]( int frame, bool lookahead )
    {
        if( frames.count( frame ) )
        {
            return;
        }
        SimFrame f;
        f.lookahead = lookahead;
        frames[frame] = f;
        upstreamQueue.push_back( frame );
    };
    const auto requestTarget = [&]( int frame )
    {
        target = frame;
        stillDrawing = true;
        auto it = frames.find( frame );
        if( it == frames.end() ) request( frame, false );
        for( int offset = 1; offset <= lookaheadDepth; ++offset ) request( frame + offset, true );
    };

    for( double now = 0.0; now <= wallMs; now += kStepMs )
    {
        const double rate = ( upstreamBusy >= 0 && renderBusy >= 0 ) ? 1.0 / contention : 1.0;
        if( upstreamBusy >= 0 ) upstreamLeft -= kStepMs * rate;
        if( renderBusy >= 0 ) renderLeft -= kStepMs * rate;
        // Upstream worker (decode + dual-ISO), one frame at a time, in request order.
        if( upstreamBusy >= 0 && upstreamLeft <= 1e-9 )
        {
            frames[upstreamBusy].stage = 2;
            renderQueue.push_back( upstreamBusy );
            upstreamBusy = -1;
            meter.upstreamEnd( now );
        }
        if( upstreamBusy < 0 && !upstreamQueue.empty()
         && playback_overlap::reconMayStart( exclusiveRecon, renderBusy >= 0, !renderQueue.empty() ) )
        {
            upstreamBusy = upstreamQueue.front();
            upstreamQueue.pop_front();
            frames[upstreamBusy].stage = 1;
            upstreamLeft = upstreamMs;
            meter.upstreamBegin( now, renderBusy >= 0 || presentScheduled );
        }
        // Render thread.
        if( renderBusy >= 0 && renderLeft <= 1e-9 )
        {
            frames[renderBusy].stage = 4;
            frames[renderBusy].doneMs = now;
            renderBusy = -1;
            meter.renderEnd( now );
            // frameReady -> drawFrameReady: present the target if it is the frame just finished...
            if( !presentScheduled && frames.count( target ) && frames[target].stage == 4 )
            {
                presentScheduled = true;
                presentAt = now + hopMs;
            }
            else if( !presentScheduled )
            {
                // ...otherwise nothing matches: recompute still-drawing (the empty-acquire path).
                stillDrawing = busyForAdvance() || ( frames.count( target ) && frames[target].stage < 5 );
            }
        }
        if( renderBusy < 0 && !renderQueue.empty() )
        {
            renderBusy = renderQueue.front();
            renderQueue.pop_front();
            frames[renderBusy].stage = 3;
            renderLeft = renderMs;
            meter.renderBegin( now, upstreamBusy >= 0 || !upstreamQueue.empty() );
        }
        int inFlight = 0;
        for( const auto &entry : frames ) if( entry.second.stage < 5 ) ++inFlight;
        if( inFlight > run.maxInFlight ) run.maxInFlight = inFlight;

        // The GUI present (drawFrameReady -> finishPresentedFrame).
        if( presentScheduled && now >= presentAt )
        {
            presentScheduled = false;
            if( lastPresented >= 0 && target != lastPresented + 1 ) run.inOrder = false;
            lastPresented = target;
            frames.erase( target );
            ++run.presented;
            if( !run.presentedAny ) run.firstPresentMs = now;
            run.presentedAny = true;
            run.lastPresentMs = now;
            stillDrawing = busyForAdvance();
        }

        // The 8 ms poll timer (timerFrameEvent): advance one whole frame when not still drawing.
        if( now >= nextPollMs )
        {
            nextPollMs += kPollMs;
            if( !stillDrawing )
            {
                if( target < 0 || guard.grantWholeFrame( now, kNativeFps ) )
                {
                    requestTarget( target + 1 );
                    if( frames[target].stage == 4 && !presentScheduled )
                    {
                        presentScheduled = true; // covered and already rendered
                        presentAt = now + hopMs;
                    }
                }
            }
        }
    }
    run.overlap = meter.snapshot( wallMs );
    return run;
}

} // namespace

TEST(PlaybackDecodeRenderOverlapPolicy, LookaheadIsOnByDefaultOnlyOnTheTextureRouteAndZeroIsTheKillSwitch)
{
    ASSERT_EQ( effectiveLookaheadFrames( -1, true ), 2 );
    ASSERT_EQ( effectiveLookaheadFrames( -1, false ), 0 );
    ASSERT_EQ( effectiveLookaheadFrames( 0, true ), 0 );
    ASSERT_EQ( effectiveLookaheadFrames( 1, false ), 1 );
    ASSERT_EQ( effectiveLookaheadFrames( 1, true ), 1 );
    ASSERT_EQ( effectiveLookaheadFrames( 9, true ), 3 );
}

TEST(PlaybackDecodeRenderOverlapPolicy, OnlySpeculativeWorkForAnotherFrameRunsBehindTheTimer)
{
    WorkItem item;
    item.generation = 7;
    item.frameNumber = 12;

    item.playbackLookahead = false;
    ASSERT_TRUE( blocksPlaybackAdvance( item, 11, 7 ) );  // a normal request always holds the timer
    item.playbackLookahead = true;
    ASSERT_TRUE( blocksPlaybackAdvance( item, 12, 7 ) );  // the frame the GUI asked for holds it
    ASSERT_FALSE( blocksPlaybackAdvance( item, 11, 7 ) ); // a frame ahead of the target does not
    ASSERT_TRUE( blocksPlaybackAdvance( item, 11, 8 ) );  // an older generation is not ours to ignore
    ASSERT_TRUE( blocksPlaybackAdvance( item, -1, 7 ) );  // no target: keep the old behaviour
}

TEST(PlaybackDecodeRenderOverlapPolicy, AReconStartsOnlyWhenTheRenderThreadHasNothingToRender)
{
    ASSERT_TRUE( playback_overlap::reconMayStart( true, false, false ) );
    ASSERT_FALSE( playback_overlap::reconMayStart( true, true, false ) );  // a render is running
    ASSERT_FALSE( playback_overlap::reconMayStart( true, false, true ) );  // a reconned frame waits to render
    ASSERT_TRUE( playback_overlap::reconMayStart( false, true, true ) );   // MLVAPP_PLAYBACK_OVERLAP_RECON_EXCLUSIVE=0
}

TEST(PlaybackDecodeRenderOverlapMeter, DecodeAndReconBusyAreSplitOut)
{
    OverlapMeter meter;
    meter.reset( 0.0 );
    meter.upstreamBegin( 0.0, false, playback_overlap::UpstreamStage::Decode );
    meter.upstreamEnd( 4.0, playback_overlap::UpstreamStage::Decode );
    meter.upstreamBegin( 4.0, false, playback_overlap::UpstreamStage::Recon );
    meter.upstreamBegin( 10.0, false, playback_overlap::UpstreamStage::Decode ); // next frame decodes under the recon
    meter.upstreamEnd( 14.0, playback_overlap::UpstreamStage::Decode );
    meter.upstreamEnd( 34.0, playback_overlap::UpstreamStage::Recon );
    meter.noteReconHeldForRender();
    const OverlapSnapshot s = meter.snapshot( 34.0 );
    ASSERT_NEAR( s.decodeBusyMs, 8.0, 1e-9 );
    ASSERT_NEAR( s.reconBusyMs, 30.0, 1e-9 );
    ASSERT_NEAR( s.upstreamBusyMs, 34.0, 1e-9 ); // the union, not the sum
    ASSERT_EQ( s.reconStartsHeldForRender, 1u );
}
TEST(PlaybackDecodeRenderOverlapMeter, SerialStagesReportNoOverlap)
{
    OverlapMeter meter;
    meter.reset( 0.0 );
    for( int frame = 0; frame < 4; ++frame )
    {
        const double t = frame * 47.0;
        meter.upstreamBegin( t, false );
        meter.upstreamEnd( t + 30.0 );
        meter.renderBegin( t + 30.0, false );
        meter.renderEnd( t + 41.0 );
    }
    const OverlapSnapshot s = meter.snapshot( 4 * 47.0 );
    ASSERT_EQ( s.upstreamStarts, 4u );
    ASSERT_EQ( s.upstreamStartsOverlapped, 0u );
    ASSERT_EQ( s.renderStarts, 4u );
    ASSERT_NEAR( s.upstreamBusyMs, 120.0, 1e-9 );
    ASSERT_NEAR( s.renderBusyMs, 44.0, 1e-9 );
   ASSERT_NEAR( s.overlapFractionOfRender(), 0.0, 1e-9 );
}

TEST(PlaybackDecodeRenderOverlapMeter, NextFrameUpstreamUnderTheRenderIsCountedAsOverlap)
{
    OverlapMeter meter;
    meter.reset( 0.0 );
    meter.upstreamBegin( 0.0, false );   // frame 0 decode + dual-ISO
    meter.upstreamEnd( 30.0 );
    meter.renderBegin( 30.0, false );    // frame 0 render...
    meter.upstreamBegin( 30.0, true );   // ...while frame 1 decodes
    meter.renderEnd( 41.0 );
    meter.upstreamEnd( 60.0 );
    meter.renderBegin( 60.0, true );     // frame 1 render, frame 2 already queued upstream
    meter.renderEnd( 71.0 );
    const OverlapSnapshot s = meter.snapshot( 71.0 );
    ASSERT_EQ( s.upstreamStartsOverlapped, 1u );
    ASSERT_EQ( s.renderStartsWithUpstreamInFlight, 1u );
    ASSERT_NEAR( s.overlapMs, 11.0, 1e-9 );
    ASSERT_NEAR( s.renderBusyMs, 22.0, 1e-9 );
    ASSERT_NEAR( s.overlapFractionOfRender(), 0.5, 1e-9 );
    ASSERT_NEAR( s.upstreamEngagement(), 0.5, 1e-9 );
    ASSERT_NEAR( s.windowMs, 71.0, 1e-9 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, OneFrameInFlightReproducesTheSerialBaseline)
{
    // depth 0 is master before this card (and the env kill switch): ~47 ms a frame, ~21 fps, no overlap.
    const PipelineRun r = simulatePipeline( 0, Gate::ExceptLookahead, 30.0, 11.0, 6.0 );
    ASSERT_TRUE( r.inOrder );
    ASSERT_TRUE( r.maxInFlight <= 1 );
    ASSERT_TRUE( r.presentedFpsAfterFirst() > 19.0 );
    ASSERT_TRUE( r.presentedFpsAfterFirst() < 21.5 );
    ASSERT_NEAR( r.overlap.overlapFractionOfRender(), 0.0, 1e-9 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, TheOldIsIdleGateHoldsTheTimerBehindTheLookahead)
{
    // The lookahead alone (env knob, old gate) never reaches native: the timer waits for the speculative frames.
    const PipelineRun r = simulatePipeline( 2, Gate::IsIdle, 30.0, 11.0, 6.0 );
    ASSERT_TRUE( r.inOrder );
    ASSERT_TRUE( r.presentedFpsAfterFirst() < 23.0 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, TwoFramesInFlightReachNativeInOrderWithOverlap)
{
    const PipelineRun r = simulatePipeline( effectiveLookaheadFrames( -1, true ), Gate::ExceptLookahead,
                                            30.0, 11.0, 6.0 );
    ASSERT_TRUE( r.inOrder );                              // exact frame order: no skip, no repeat
    ASSERT_TRUE( r.maxInFlight >= 2 );
    ASSERT_TRUE( r.presentedFpsAfterFirst() > 23.8 );         // native 23.976...
    ASSERT_TRUE( r.presentedFpsAfterFirst() <= kNativeFps + 0.05 ); // ...and never faster (#261)
    // At native pace the stages have slack, so they idle between frames and the render-time overlap is small; what
    // two frames in flight buy here is that each frame's upstream started a frame early (engagement), not overlap.
    ASSERT_TRUE( r.overlap.overlapFractionOfRender() > 0.01 );
    ASSERT_TRUE( r.overlap.upstreamEngagement() > 0.9 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, ASlowUpstreamIsBoundByItsOwnStageNotTheSum)
{
    // Upstream 45 ms is slower than native (41.7 ms). In series a frame costs 45 + 11 + 6 = 62 ms (~16 fps); with two
    // frames in flight the period approaches the slowest stage (~22 fps).
    const PipelineRun serial = simulatePipeline( 0, Gate::ExceptLookahead, 45.0, 11.0, 6.0 );
    const PipelineRun overlapped = simulatePipeline( 2, Gate::ExceptLookahead, 45.0, 11.0, 6.0 );
    ASSERT_TRUE( overlapped.inOrder );
    ASSERT_TRUE( serial.presentedFpsAfterFirst() < 16.5 );
    ASSERT_TRUE( overlapped.presentedFpsAfterFirst() > 20.5 );
    // Here the upstream is the bottleneck, so every render runs under the next frame's decode + dual-ISO.
    ASSERT_TRUE( overlapped.overlap.overlapFractionOfRender() > 0.9 );
    ASSERT_NEAR( serial.overlap.overlapFractionOfRender(), 0.0, 1e-9 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, TheExclusiveReconStartRemovesContentionAndKeepsNativeInOrder)
{
    // Two OpenMP teams at once each run at 1/1.6 speed. The exclusive start never runs them together, so the stages
    // keep their own costs, frames stay in order and the timeline holds native; without it the render runs contended.
    const PipelineRun freeOverlap = simulatePipeline( 2, Gate::ExceptLookahead, 30.0, 11.0, 6.0, 1.6, false );
    const PipelineRun exclusive = simulatePipeline( 2, Gate::ExceptLookahead, 30.0, 11.0, 6.0, 1.6, true );
    ASSERT_TRUE( exclusive.inOrder );
    ASSERT_TRUE( exclusive.maxInFlight >= 2 );
    ASSERT_NEAR( exclusive.overlap.overlapFractionOfRender(), 0.0, 1e-9 );
    ASSERT_TRUE( freeOverlap.overlap.overlapFractionOfRender() > 0.1 );
    ASSERT_TRUE( exclusive.overlap.renderBusyMs < freeOverlap.overlap.renderBusyMs );
    ASSERT_TRUE( exclusive.presentedFpsAfterFirst() > 23.8 );
    ASSERT_TRUE( exclusive.presentedFpsAfterFirst() <= kNativeFps + 0.05 );
}
// ---- wiring: MainWindow.cpp needs a full GUI build, so its source is read as text --------------------------
namespace
{
QString readRepoText( const char *relative )
{
    QFile file( repo_file_path( QString::fromLatin1( relative ) ) );
    if( !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    return QTextStream( &file ).readAll();
}
} // namespace

TEST(PlaybackDecodeRenderOverlapWiring, ThePresentPathsRecomputeStillDrawingWithTheLookaheadAwareGate)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    ASSERT_FALSE( mw.isEmpty() );
    // The playback advance asks the lookahead-aware idle check, never plain isIdle(), after a present.
    ASSERT_TRUE( mw.contains( QStringLiteral("isIdleExceptPlaybackLookahead(") ) );
    ASSERT_TRUE( mw.count( QStringLiteral("renderThreadBusyForPlaybackAdvance( currentPlaybackAdvanceTarget() )") ) >= 10 );
    const int drawReady = mw.indexOf( QStringLiteral("void MainWindow::drawFrameReady()") );
    ASSERT_TRUE( drawReady > 0 );
    const int drawReadyEnd = mw.indexOf( QStringLiteral("\nvoid MainWindow::"), drawReady + 10 );
    const QString drawReadyBody = mw.mid( drawReady, drawReadyEnd - drawReady );
    ASSERT_FALSE( drawReadyBody.contains( QStringLiteral("m_frameStillDrawing = !m_pRenderThread->isIdle();") ) );
    ASSERT_FALSE( drawReadyBody.contains( QStringLiteral("m_frameStillDrawing = m_pRenderThread && !m_pRenderThread->isIdle();") ) );
    // The depth comes from the policy (texture-route default, env override), and the counter is printed.
    ASSERT_TRUE( mw.contains( QStringLiteral("playback_overlap::effectiveLookaheadFrames(") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("playback_smoke.overlap_summary") ) );
    ASSERT_FALSE( mw.contains( QStringLiteral("mlvappPlaybackRenderLookaheadFrames()") ) );
}

TEST(PlaybackDecodeRenderOverlapWiring, TheRenderThreadReportsEveryStageToTheMeter)
{
    const QString rft = readRepoText( "platform/qt/RenderFrameThread.cpp" );
    ASSERT_FALSE( rft.isEmpty() );
    ASSERT_EQ( rft.count( QStringLiteral("m_overlapMeter.upstreamBegin(") ), 2 ); // decode and recon takes
    ASSERT_EQ( rft.count( QStringLiteral("m_overlapMeter.upstreamEnd(") ), 2 );   // decode and recon done
    ASSERT_EQ( rft.count( QStringLiteral("m_overlapMeter.renderBegin(") ), 1 );
    ASSERT_EQ( rft.count( QStringLiteral("m_overlapMeter.renderEnd(") ), 1 );
    ASSERT_EQ( rft.count( QStringLiteral("playback_overlap::UpstreamStage::Decode") ), 2 );
    ASSERT_EQ( rft.count( QStringLiteral("playback_overlap::UpstreamStage::Recon") ), 2 );
    ASSERT_TRUE( rft.contains( QStringLiteral("playback_overlap::reconMayStart(") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("m_reconWaitCondition.wakeAll();\n        m_mutex.unlock();\n        emit frameReady();") ) );
    ASSERT_TRUE( rft.contains( QStringLiteral("playback_overlap::blocksPlaybackAdvance(") ) );
}

// ---- CPU-S1-PACE-POST-785-LEVER-1: the CPU-route lookahead (depth 1), its admission and its telemetry ----------

TEST(PlaybackLookaheadCpuRoute, ThreeArgumentDepthEnvWinsTextureTwoCpuOneElseZero)
{
    const int envs[] = { -1, 0, 1, 2, 5 };
    for( const int env : envs )
    {
        for( int tex = 0; tex < 2; ++tex )
        {
            for( int cpu = 0; cpu < 2; ++cpu )
            {
                int expected = 0;
                if( env >= 0 ) expected = env > 3 ? 3 : env;
                else if( tex ) expected = 2;
                else if( cpu ) expected = 1;
                ASSERT_EQ( expected, effectiveLookaheadFrames( env, tex != 0, cpu != 0 ) );
            }
            // The 2-argument form is unchanged: the 3-argument form with the CPU route refused.
            ASSERT_EQ( effectiveLookaheadFrames( env, tex != 0, false ), effectiveLookaheadFrames( env, tex != 0 ) );
        }
    }
    ASSERT_EQ( 2, playback_overlap::kDefaultTextureRouteLookaheadFrames );
    ASSERT_EQ( 1, playback_overlap::kDefaultCpuRouteLookaheadFrames );
    ASSERT_EQ( 2, effectiveLookaheadFrames( -1, true, true ) );   // the texture default wins over the CPU one
    ASSERT_EQ( 0, effectiveLookaheadFrames( 0, true, true ) );    // env 0 kills both routes
    ASSERT_EQ( 0, effectiveLookaheadFrames( 0, false, true ) );
    ASSERT_TRUE( QString::fromLatin1( playback_overlap::lookaheadRouteName( true, true ) ) == QStringLiteral("texture") );
    ASSERT_TRUE( QString::fromLatin1( playback_overlap::lookaheadRouteName( false, true ) ) == QStringLiteral("cpu") );
    ASSERT_TRUE( QString::fromLatin1( playback_overlap::lookaheadRouteName( false, false ) ) == QStringLiteral("none") );
}

namespace
{
PlaybackLookaheadCpuRouteInputs admittedCpuRoute()
{
    PlaybackLookaheadCpuRouteInputs in;
    in.playing = true;
    in.fileLoaded = true;
    in.dropFrameMode = true;
    in.phase3DecodeReconProcess = true;
    in.prepRoute.playing = true;
    in.prepRoute.displayPreviewCachingAllowed = false;
    in.prepRoute.cpuPreviewProcessingActive = true;
    in.prepRoute.outputProcessed8 = false;
    return in;
}
} // namespace

TEST(PlaybackLookaheadCpuRoute, AdmissionTruthTableRefusesEveryGpuRouteAndEveryMissingCondition)
{
    ASSERT_TRUE( playbackLookaheadCpuRouteAdmits( admittedCpuRoute() ) );
    struct Flip { const char *name; void ( *apply )( PlaybackLookaheadCpuRouteInputs & ); };
    const Flip refusals[] = {
        { "not playing", []( PlaybackLookaheadCpuRouteInputs &i ) { i.playing = false; } },
        { "no file", []( PlaybackLookaheadCpuRouteInputs &i ) { i.fileLoaded = false; } },
        { "whole-frame playback", []( PlaybackLookaheadCpuRouteInputs &i ) { i.dropFrameMode = false; } },
        { "no phase 3 recon worker", []( PlaybackLookaheadCpuRouteInputs &i ) { i.phase3DecodeReconProcess = false; } },
        { "GPU texture route", []( PlaybackLookaheadCpuRouteInputs &i ) { i.textureRouteRequested = true; } },
        { "GPU bilinear debayer", []( PlaybackLookaheadCpuRouteInputs &i ) { i.gpuBilinearDebayer = true; } },
        { "GPU AMaZE debayer", []( PlaybackLookaheadCpuRouteInputs &i ) { i.gpuAmazeDebayer = true; } },
        { "gpu16 preview", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.gpu16PreviewActive = true; } },
        { "GPU preview processing", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.gpuPreviewProcessingActive = true; } },
        { "AMaZE texture candidate", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.gpuAmazeTexturePresentCandidate = true; } },
        { "recon texture candidate", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.gpuPlaybackReconTexturePresentCandidate = true; } },
        { "no-readback candidate", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.gpuPlaybackReconTextureNoReadbackCandidate = true; } },
        { "prep not playing", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.playing = false; } },
        { "display caching (paused policy)", []( PlaybackLookaheadCpuRouteInputs &i ) { i.prepRoute.displayPreviewCachingAllowed = true; } },
        { "8-bit output, no CPU processing", []( PlaybackLookaheadCpuRouteInputs &i ) {
              i.prepRoute.cpuPreviewProcessingActive = false; i.prepRoute.outputProcessed8 = true; } },
    };
    for( const Flip &f : refusals )
    {
        PlaybackLookaheadCpuRouteInputs in = admittedCpuRoute();
        f.apply( in );
        if( playbackLookaheadCpuRouteAdmits( in ) )
        {
            fprintf( stderr, "admitted with: %s\n", f.name );
            ASSERT_TRUE( false );
        }
    }
    // The buffer fields are never the route: a null buffer on the request side still admits.
    PlaybackLookaheadCpuRouteInputs noBuffer = admittedCpuRoute();
    noBuffer.prepRoute.rawImage16 = nullptr;
    noBuffer.prepRoute.rawImage16Bytes = 0;
    noBuffer.prepRoute.sourceImage16Bytes = 0;
    ASSERT_TRUE( playbackLookaheadCpuRouteAdmits( noBuffer ) );
}

TEST(PlaybackLookaheadCpuRoute, TheCpuPrepRouteIsDecidedByTheBorrowPredicateItself)
{
    // Every combination of #774's nine route bools: with the lookahead's own gates open, the admission equals
    // playbackPrepMayBorrowSlotRgb16 on the same route with an adequate buffer.
    static const uint16_t words[4] = {};
    for( int mask = 0; mask < ( 1 << 9 ); ++mask )
    {
        PlaybackLookaheadCpuRouteInputs in = admittedCpuRoute();
        PlaybackPrepBorrowInputs &r = in.prepRoute;
        r.playing = ( mask & 1 ) != 0;
        r.displayPreviewCachingAllowed = ( mask & 2 ) != 0;
        r.cpuPreviewProcessingActive = ( mask & 4 ) != 0;
        r.outputProcessed8 = ( mask & 8 ) != 0;
        r.gpu16PreviewActive = ( mask & 16 ) != 0;
        r.gpuPreviewProcessingActive = ( mask & 32 ) != 0;
        r.gpuAmazeTexturePresentCandidate = ( mask & 64 ) != 0;
        r.gpuPlaybackReconTexturePresentCandidate = ( mask & 128 ) != 0;
        r.gpuPlaybackReconTextureNoReadbackCandidate = ( mask & 256 ) != 0;
        PlaybackPrepBorrowInputs withBuffer = r;
        withBuffer.rawImage16 = words;
        withBuffer.rawImage16Bytes = sizeof( words );
        withBuffer.sourceImage16Bytes = sizeof( words );
        ASSERT_EQ( playbackPrepMayBorrowSlotRgb16( withBuffer ), playbackLookaheadCpuRouteAdmits( in ) );
    }
}

TEST(PlaybackLookaheadCpuRoute, TheMaxBusyGapNamesItsDominantBucket)
{
    using gui_present_gap::OtherBusySpan;
    using gui_present_gap::OtherBusySpans;
    using gui_present_gap::SpanClock;
    OtherBusySpans spans;
    spans.openGap();
    ASSERT_EQ( -1, spans.dominantBucket( 10.0 ) );   // nothing named: all unattributed
    SpanClock a; a.gapSerial = 1; a.nowNs = 0;
    SpanClock b = a; b.nowNs = 6000000;              // 6 ms of draw_frame_ready
    spans.begin( a );
    spans.end( OtherBusySpan::DrawFrameReady, b );
    ASSERT_EQ( static_cast<int>( OtherBusySpan::DrawFrameReady ), spans.dominantBucket( 10.0 ) );  // 6 > 4 rest
    ASSERT_EQ( -1, spans.dominantBucket( 20.0 ) );   // 14 ms unattributed beats 6
    ASSERT_EQ( static_cast<int>( OtherBusySpan::DrawFrameReady ), spans.dominantBucket( 3.0 ) );   // scaled to the budget
    ASSERT_EQ( -1, spans.dominantBucket( std::numeric_limits<double>::quiet_NaN() ) );   // no budget: nothing charged
    spans.closeGap( 10.0 );
    ASSERT_EQ( -1, spans.dominantBucket( 10.0 ) );   // closeGap cleared the gap's spans
    ASSERT_TRUE( QString::fromLatin1( gui_present_gap::otherBusyBucketName( -1 ) ) == QStringLiteral("unattributed") );
    ASSERT_TRUE( QString::fromLatin1( gui_present_gap::otherBusyBucketName( -2 ) ) == QStringLiteral("none") );
    ASSERT_TRUE( QString::fromLatin1( gui_present_gap::otherBusyBucketName( 99 ) ) == QStringLiteral("none") );
    ASSERT_TRUE( QString::fromLatin1( gui_present_gap::otherBusyBucketName( 0 ) ) == QStringLiteral("draw_frame_ready") );
}

namespace
{
QString functionBody( const QString &source, const QString &signature )
{
    const int start = source.indexOf( signature );
    if( start < 0 ) return QString();
    const int end = source.indexOf( QStringLiteral("\n}\n"), start );
    return end < 0 ? QString() : source.mid( start, end - start );
}
} // namespace

TEST(PlaybackLookaheadCpuRouteWiring, DrawFrameConsultsThePredicateBesideTheTextureAdmission)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    ASSERT_FALSE( mw.isEmpty() );
    const QString draw = functionBody( mw, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )") );
    ASSERT_FALSE( draw.isEmpty() );
    const int texture = draw.indexOf( QStringLiteral("m_playbackLookaheadTextureRouteAdmitted =\n        ui->actionPlay->isChecked()") );
    const int cpu = draw.indexOf( QStringLiteral("m_playbackLookaheadCpuRouteAdmitted = playbackLookaheadCpuRouteAdmits( cpuRoute );") );
    const int prune = draw.indexOf( QStringLiteral("if( playbackRenderLookaheadFrames() > 0\n     && ui->actionPlay->isChecked()") );
    ASSERT_TRUE( texture > 0 );
    ASSERT_TRUE( cpu > texture );
    ASSERT_TRUE( prune > cpu );
    // The CPU prep route is #774's predicate on the request's inputs, not a restatement.
    ASSERT_TRUE( draw.contains( QStringLiteral("cpuRoute.prepRoute.cpuPreviewProcessingActive =\n            requestContext.renderThreadUsingCpuPreviewProcessing\n            && requestContext.gpuPreviewProcessingConfig.enabled;") ) );
    ASSERT_TRUE( draw.contains( QStringLiteral("cpuRoute.dropFrameMode = ui->actionDropFrameMode->isChecked();") ) );
    ASSERT_TRUE( draw.contains( QStringLiteral("requestedPhase3Mode == Phase3Mode::DecodeReconProcess\n            && !phase3LiveFallbackActive()\n            && !phase3KillSwitchActive( requestedPhase3Mode );") ) );
    const QString depth = functionBody( mw, QStringLiteral("int MainWindow::playbackRenderLookaheadFrames() const") );
    ASSERT_TRUE( depth.contains( QStringLiteral("m_playbackLookaheadTextureRouteAdmitted,\n                                                       m_playbackLookaheadCpuRouteAdmitted );") ) );
    const QString hdr = readRepoText( "platform/qt/PlaybackLookaheadCpuRoute.h" );
    ASSERT_TRUE( hdr.contains( QStringLiteral("return playbackPrepMayBorrowSlotRgb16( route );") ) );
}

TEST(PlaybackLookaheadCpuRouteWiring, TheQueueAdmitsTheCpuRouteAndKeepsTheTextureChecks)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    const QString queue = functionBody( mw, QStringLiteral("void MainWindow::queuePlaybackLookaheadRequests(") );
    ASSERT_FALSE( queue.isEmpty() );
    ASSERT_TRUE( queue.contains( QStringLiteral(
        "    const bool textureRouteAdmits =\n"
        "        baseContext.gpuPlaybackReconTexturePresentRequested\n"
        "        && baseContext.gpuPlaybackReconAmazeTexturePresentAdmitted\n"
        "        && mainWindowGpuTextureRouteAdmitsPlaybackScale(\n"
        "               baseContext.playbackScaleFactor, gpuReducedReconHonouredSessionScale() );\n"
        "    if( !textureRouteAdmits && !m_playbackLookaheadCpuRouteAdmitted ) return;") ) );
    ASSERT_TRUE( queue.contains( QStringLiteral("const int depth = playbackRenderLookaheadFrames();") ) );
    ASSERT_TRUE( queue.contains( QStringLiteral("lookaheadContext.playbackLookaheadRequest = true;") ) );
}

TEST(PlaybackLookaheadCpuRouteWiring, TheCpuAcquireIsTargetFiltered)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    const QString ready = functionBody( mw, QStringLiteral("void MainWindow::drawFrameReady()") );
    ASSERT_FALSE( ready.isEmpty() );
    // With a lookahead depth (any route) the CPU slot is taken only by the target-filtered acquire.
    ASSERT_TRUE( ready.contains( QStringLiteral("const bool targetAwareLookaheadAcquire =\n        playbackRenderLookaheadFrames() > 0 && ui->actionPlay->isChecked();") ) );
    ASSERT_TRUE( ready.contains( QStringLiteral(
        "            haveReadyFrame = targetAwareLookaheadAcquire\n"
        "                ? m_pRenderThread->acquireLatestReadyFrameForPlaybackTarget(") ) );
    const QString rft = readRepoText( "platform/qt/RenderFrameThread.cpp" );
    const QString acquire = functionBody( rft, QStringLiteral("bool RenderFrameThread::acquireLatestReadyFrameForPlaybackTarget(") );
    ASSERT_TRUE( acquire.contains( QStringLiteral(
        "        if( slot.presentationContext.playbackLookaheadRequest\n"
        "         && ( activePlaybackTarget < 0\n"
        "           || slot.presentationContext.presentationGeneration != activeGeneration\n"
        "           || static_cast<int>( slot.frameNumber ) != activePlaybackTarget ) )") ) );
    // The drop-frame erase counts the lookahead requests it removes (lookahead_dropped_queued).
    ASSERT_TRUE( rft.contains( QStringLiteral(
        "                if( it->presentationContext.playbackLookaheadRequest )\n"
        "                    m_playbackLookaheadDroppedQueued.fetch_add( 1, std::memory_order_relaxed );\n"
        "                it = m_renderRequests.erase( it );") ) );
}

TEST(PlaybackLookaheadCpuRouteWiring, TheRouteIsLatchedOnlyWhereALookaheadRequestIsIssued)
{
    // Adversary A1 on e35b8030: Cut In = Cut Out = 100 (one-frame loop), slider 99, Play + Loop + Drop Frame on the admitted
    // CPU route. Depth 1's candidate wraps to the current frame and is skipped, so nothing is queued (lookahead_requests=0),
    // yet the route latched at admission in drawFrame printed lookahead_route=cpu. The route is the one a request ran on.
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    const QString draw = functionBody( mw, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )") );
    ASSERT_FALSE( draw.isEmpty() );
    ASSERT_FALSE( draw.contains( QStringLiteral("m_playbackLookaheadSessionRoute =") ) );
    const QString queue = functionBody( mw, QStringLiteral("void MainWindow::queuePlaybackLookaheadRequests(") );
    const int issued = queue.indexOf( QStringLiteral("++m_playbackLookaheadRequestsIssued;") );
    const int latch = queue.indexOf( QStringLiteral(
        "m_playbackLookaheadSessionRoute =\n"
        "                playback_overlap::lookaheadRouteName( textureRouteAdmits, !textureRouteAdmits );") );
    ASSERT_TRUE( issued > 0 );
    ASSERT_TRUE( latch > issued );
    ASSERT_EQ( 1, mw.count( QStringLiteral("m_playbackLookaheadSessionRoute =") ) - mw.count( QStringLiteral("m_playbackLookaheadSessionRoute = \"none\";") ) );
}

TEST(PlaybackLookaheadCpuRouteWiring, TheMaxBusyGapStartIsStampedAtOpenNotReadAgainAtClose)
{
    // Adversary A1 on fb4d13fe: smoke start 100.000 s, the gap opens at 101.000 s and closes at 101.020 s (20 ms), then the GUI
    // thread is descheduled 500 ms before a second clock read at the close: start = 101.520 - 0.020 = 1500 ms, outside the gap
    // [1000, 1020]. The start is the opening instant, stamped once in openGuiPresentGap.
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    const QString open = functionBody( mw, QStringLiteral("void MainWindow::openGuiPresentGap( void )") );
    const QString close = functionBody( mw, QStringLiteral("void MainWindow::closeGuiPresentGap( void )") );
    ASSERT_FALSE( open.isEmpty() );
    ASSERT_FALSE( close.isEmpty() );
    const int stamp = open.indexOf( QStringLiteral(
        "m_guiPresentGapStartSinceSmokeMs = ( mlv_stage_timing_now() - m_playbackSmokeStartTime ) * 1000.0;") );
    const int armed = open.indexOf( QStringLiteral("if( !m_playbackSmokeActive || !m_guiPresentIdleProbeConnected ) return;") );
    const int start = open.indexOf( QStringLiteral("m_guiPresentGapStartNs = m_guiPresentGapClock.nsecsElapsed();") );
    ASSERT_TRUE( armed > 0 );
    ASSERT_TRUE( stamp > armed );
    ASSERT_TRUE( start > stamp );
    ASSERT_TRUE( close.contains( QStringLiteral("gaps.busyMaxAtMs = m_guiPresentGapStartSinceSmokeMs;") ) );
    ASSERT_FALSE( close.contains( QStringLiteral("mlv_stage_timing_now()") ) );   // no second clock at the close
}

TEST(PlaybackLookaheadCpuRouteWiring, TheSevenNewFieldsAreAppendedNeverRenamed)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    const int overlap = mw.indexOf( QStringLiteral("\"playback_smoke.overlap_summary session=%1 lookahead_depth=%2 \"") );
    ASSERT_TRUE( overlap > 0 );
    const int raw = mw.indexOf( QStringLiteral("            + rawPrefetchFields\n"), overlap );
    const int appended = mw.indexOf( QStringLiteral(
        "QStringLiteral( \" lookahead_route=%1 lookahead_presented=%2 lookahead_pruned=%3 \"\n"
        "                              \"lookahead_dropped_queued=%4\" )"), overlap );
    ASSERT_TRUE( raw > overlap );
    ASSERT_TRUE( appended > raw );
    ASSERT_TRUE( mw.contains( QStringLiteral("\"recon_starts_held_for_render=%19\" )") ) );   // the old format is intact
    const int gui = mw.indexOf( QStringLiteral("\"playback_smoke.gui_present_summary session=%1 frames=%2 \"") );
    const int unattributed = mw.indexOf( QStringLiteral("QStringLiteral( \" avg_gui_other_busy_unattributed_ms=%1\" )"), gui );
    const int freeze = mw.indexOf( QStringLiteral(
        "QStringLiteral( \" max_gui_busy_at_ms=%1 max_gui_busy_frame=%2 max_gui_other_busy_bucket=%3\" )") );
    ASSERT_TRUE( gui > 0 );
    ASSERT_TRUE( unattributed > 0 );
    ASSERT_TRUE( freeze > unattributed );
    const QStringList fields = { QStringLiteral("lookahead_route="), QStringLiteral("lookahead_presented="),
                                 QStringLiteral("lookahead_pruned="), QStringLiteral("lookahead_dropped_queued="),
                                 QStringLiteral("max_gui_busy_at_ms="), QStringLiteral("max_gui_busy_frame="),
                                 QStringLiteral("max_gui_other_busy_bucket=") };
    ASSERT_EQ( 7, fields.size() );
    for( const QString &f : fields ) ASSERT_EQ( 1, mw.count( QStringLiteral(" ") + f ) );
    // Fed where the spec says: the session reset, the present, both prune sites, the gap close.
    ASSERT_TRUE( mw.contains( QStringLiteral("if( requestContext.playbackLookaheadRequest ) ++m_playbackLookaheadPresented;") ) );
    ASSERT_EQ( 2, mw.count( QStringLiteral("m_playbackLookaheadPruned += static_cast<uint64_t>(") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("gaps.busyMaxBucket = m_guiOtherBusySpans.dominantBucket( split.otherBusyMs );") ) );
}
