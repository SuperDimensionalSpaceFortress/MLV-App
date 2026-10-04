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

#include <QFile>
#include <QString>
#include <QTextStream>

#include <deque>
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

// Every-frame mode (grantWholeFrame), Loop off, wallMs of playback.
PipelineRun simulatePipeline( int lookaheadDepth, Gate gate, double upstreamMs, double renderMs, double hopMs,
                              double wallMs = 25000.0 )
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
    double upstreamEnd = 0.0;
    double renderEnd = 0.0;

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
        // Upstream worker (decode + dual-ISO), one frame at a time, in request order.
        if( upstreamBusy >= 0 && now >= upstreamEnd )
        {
            frames[upstreamBusy].stage = 2;
            renderQueue.push_back( upstreamBusy );
            upstreamBusy = -1;
            meter.upstreamEnd( now );
        }
        if( upstreamBusy < 0 && !upstreamQueue.empty() )
        {
            upstreamBusy = upstreamQueue.front();
            upstreamQueue.pop_front();
            frames[upstreamBusy].stage = 1;
            upstreamEnd = now + upstreamMs;
            meter.upstreamBegin( now, renderBusy >= 0 || presentScheduled );
        }
        // Render thread.
        if( renderBusy >= 0 && now >= renderEnd )
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
            renderEnd = now + renderMs;
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
    ASSERT_TRUE( rft.contains( QStringLiteral("playback_overlap::blocksPlaybackAdvance(") ) );
}
