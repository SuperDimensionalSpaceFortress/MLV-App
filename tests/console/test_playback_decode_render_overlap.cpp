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
#include "../../platform/qt/PlaybackPrepPresentationPolicy.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <algorithm>
#include <deque>
#include <map>
#include <set>
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
    int wastedUpstream = 0; // frames whose decode + recon finished but that were never presented (skipped targets)
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
// targetStride > 1 models drop-frame playback that falls behind: each advance moves the target by the stride (an
// average; 2.5 alternates +2/+3) as soon as the GUI is free, and queued lookahead frames the target passed are pruned
// (prunePlaybackLookaheadOutsideForwardWindow); ones already in a worker run to completion and are wasted.
PipelineRun simulatePipeline( int lookaheadDepth, Gate gate, double upstreamMs, double renderMs, double hopMs,
                              double contention = 1.0, bool exclusiveRecon = false, double wallMs = 25000.0,
                              double targetStride = 1.0 )
{
    const bool dropFrameTimeline = targetStride > 1.0;
    double timelinePos = 0.0;
    std::set<int> upstreamDone;
    std::set<int> presentedFrames;
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
        for( auto it = upstreamQueue.begin(); it != upstreamQueue.end(); )
        {
            if( *it < frame && frames[*it].lookahead )
            {
                frames.erase( *it );
                it = upstreamQueue.erase( it );
            }
            else
            {
                ++it;
            }
        }
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
            upstreamDone.insert( upstreamBusy );
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
            presentedFrames.insert( target );
            for( auto it = frames.begin(); it != frames.end(); )
            {
                // Ready frames the target passed are released (the prune's ready-slot half).
                if( it->first < target && it->second.stage == 4 ) it = frames.erase( it );
                else ++it;
            }
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
                if( target < 0 || dropFrameTimeline || guard.grantWholeFrame( now, kNativeFps ) )
                {
                    int next = target + 1;
                    if( dropFrameTimeline && target >= 0 )
                    {
                        timelinePos += targetStride;
                        next = std::max( next, static_cast<int>( timelinePos ) );
                    }
                    requestTarget( next );
                    if( frames[target].stage == 4 && !presentScheduled )
                    {
                        presentScheduled = true; // covered and already rendered
                        presentAt = now + hopMs;
                    }
                }
            }
        }
    }
    for( const int frame : upstreamDone )
    {
        if( frame < lastPresented && !presentedFrames.count( frame ) ) ++run.wastedUpstream;
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

// PLAYBACK-CPU-PACE-GAP-1: the CPU route gets depth 1 by default when cpuRouteLookaheadDecision() admits it; the
// texture-route default and the env override are unchanged.
TEST(PlaybackDecodeRenderOverlapPolicy, TheCpuRouteDefaultIsDepthOneAndNeverOutranksTheEnvOrTheTextureRoute)
{
    ASSERT_EQ( effectiveLookaheadFrames( -1, false, true ), 1 );
    ASSERT_EQ( effectiveLookaheadFrames( -1, true, true ), 2 );
    ASSERT_EQ( effectiveLookaheadFrames( 0, false, true ), 0 );  // MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=0
    ASSERT_EQ( effectiveLookaheadFrames( 2, false, false ), 2 );
    ASSERT_EQ( effectiveLookaheadFrames( -1, false, false ), 0 );
    ASSERT_EQ( playback_overlap::kDefaultCpuRouteLookaheadFrames, 1 );
    ASSERT_EQ( playback_overlap::kDefaultTextureRouteLookaheadFrames, 2 );
}

TEST(PlaybackDecodeRenderOverlapPolicy, EveryCpuRouteRefusalHasItsOwnReason)
{
    using playback_overlap::CpuLookaheadDecision;
    using playback_overlap::CpuLookaheadRefusal;
    using playback_overlap::cpuRouteLookaheadDecision;
    // (killSwitch, playing, textureRoute, phase3ReconWorker, gpuPreviewRoute, scale, explicitEnv)
    CpuLookaheadDecision d = cpuRouteLookaheadDecision( false, true, false, true, false, 2, false );
    ASSERT_TRUE( d.eligible );
    ASSERT_TRUE( d.defaultAdmitted );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::None );
    d = cpuRouteLookaheadDecision( false, true, false, true, false, 4, false );
    ASSERT_TRUE( d.defaultAdmitted );

    // The kill switch beats everything, including an explicit env and an otherwise admitted s2.
    d = cpuRouteLookaheadDecision( true, true, false, true, false, 2, true );
    ASSERT_FALSE( d.eligible );
    ASSERT_FALSE( d.defaultAdmitted );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::KillSwitch );
    d = cpuRouteLookaheadDecision( true, false, true, false, true, 1, false );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::KillSwitch );

    d = cpuRouteLookaheadDecision( false, false, false, true, false, 2, false );
    ASSERT_FALSE( d.eligible );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::NotPlaying );
    d = cpuRouteLookaheadDecision( false, true, true, true, false, 2, false );
    ASSERT_FALSE( d.eligible );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::TextureRoute );
    d = cpuRouteLookaheadDecision( false, true, false, false, false, 2, false );
    ASSERT_FALSE( d.eligible );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::NotPhase3 );
    d = cpuRouteLookaheadDecision( false, true, false, true, true, 2, false );
    ASSERT_FALSE( d.eligible );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::GpuPreviewRoute );

    // s1 refuses only the default: without an env it is refused, with an explicit env it still runs.
    d = cpuRouteLookaheadDecision( false, true, false, true, false, 1, false );
    ASSERT_TRUE( d.eligible );
    ASSERT_FALSE( d.defaultAdmitted );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::ScaleOneDefault );
    ASSERT_EQ( effectiveLookaheadFrames( -1, false, d.defaultAdmitted ), 0 );
    d = cpuRouteLookaheadDecision( false, true, false, true, false, 1, true );
    ASSERT_TRUE( d.eligible );
    ASSERT_FALSE( d.defaultAdmitted );
    ASSERT_TRUE( d.refusal == CpuLookaheadRefusal::None );
    ASSERT_EQ( effectiveLookaheadFrames( 1, false, d.defaultAdmitted ), 1 );

    // Every reason prints a distinct name (the overlap_summary counts are keyed by it).
    for( int a = 0; a < static_cast<int>( CpuLookaheadRefusal::Count ); ++a )
    {
        for( int b = a + 1; b < static_cast<int>( CpuLookaheadRefusal::Count ); ++b )
        {
            ASSERT_TRUE( QString::fromLatin1( playback_overlap::cpuLookaheadRefusalName(
                             static_cast<CpuLookaheadRefusal>( a ) ) )
                         != QString::fromLatin1( playback_overlap::cpuLookaheadRefusalName(
                             static_cast<CpuLookaheadRefusal>( b ) ) ) );
        }
    }
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
TEST(PlaybackDecodeRenderOverlapPipeline, OneCpuLookaheadFramePipelinesTheReconWorker)
{
    // PLAYBACK-CPU-PACE-GAP-1, the Bachelor CPU display shape: recon 41 ms, render 3 ms, ~20 ms tick + GUI hop,
    // native 41.7 ms. In series a frame costs ~64 ms (s2 measured 67.2); with the CPU default (depth 1) the recon of
    // N+1 runs while N renders and hops, so the period approaches the recon itself.
    const int cpuDepth = effectiveLookaheadFrames( -1, false, true );
    ASSERT_EQ( cpuDepth, 1 );
    const PipelineRun serial = simulatePipeline( 0, Gate::ExceptLookahead, 41.0, 3.0, 20.0 );
    const PipelineRun pipelined = simulatePipeline( cpuDepth, Gate::ExceptLookahead, 41.0, 3.0, 20.0 );
    ASSERT_TRUE( serial.inOrder );
    ASSERT_TRUE( pipelined.inOrder );                    // every-frame playback still presents N, N+1, ... exactly
    ASSERT_TRUE( serial.presentedFpsAfterFirst() < 16.5 );
    ASSERT_TRUE( pipelined.presentedFpsAfterFirst() > 1.25 * serial.presentedFpsAfterFirst() );
    ASSERT_TRUE( pipelined.presentedFpsAfterFirst() <= kNativeFps + 0.05 ); // never faster than native (#261)
    ASSERT_EQ( pipelined.wastedUpstream, 0 );
}

TEST(PlaybackDecodeRenderOverlapPipeline, AtAStrideOfTwoAndAHalfTheLookaheadFrameIsWastedWhichIsWhyS1RefusesIt)
{
    // s1 is prep-bound (~100 ms a frame) and drop-frame skips ~2.5 frames per present, so the frame after the target
    // is almost never the next one shown: its recon is pure waste, and it holds the worker the next target needs.
    const double kWallMs = 25000.0;
    const PipelineRun serial =
        simulatePipeline( 0, Gate::ExceptLookahead, 41.0, 3.0, 56.0, 1.0, false, kWallMs, 2.5 );
    const PipelineRun lookahead =
        simulatePipeline( 1, Gate::ExceptLookahead, 41.0, 3.0, 56.0, 1.0, false, kWallMs, 2.5 );
    ASSERT_TRUE( serial.presented > 50 );
    ASSERT_EQ( serial.wastedUpstream, 0 );
    ASSERT_TRUE( lookahead.wastedUpstream * 2 >= lookahead.presented ); // at least every other present wasted a recon
    ASSERT_TRUE( lookahead.presentedFpsAfterFirst() <= serial.presentedFpsAfterFirst() * 1.02 ); // and bought nothing
}

// ---- PLAYBACK-CPU-PACE-GAP-1 r3: drop-frame off presents every frame, lookahead or not ----------------------------
// r2 on Bachelor, drop-frame off: with the CPU lookahead the render side outran prep + present, and the prep queue
// conflated by serial (a new task replaced the pending one, a finished result discarded the unpresented one), so arm A
// showed 399 of 599 frames at s2 (prep_replaced_after=123). The fake pipeline below has the same four stages -- render
// (one lookahead frame), the prep worker, the GUI present and the native-paced advance -- and runs them on the policy
// MainWindow uses.
namespace
{

struct DropOffRun
{
    int presented = 0;
    bool inOrder = true;
    int maxPrepBacklog = 0;
    double elapsedMs = 0.0;
    uint64_t holdTimeouts = 0;
};

DropOffRun simulateDropOffPlayback( bool r3Policy, int frames, double renderMs, double prepMs, double presentMs )
{
    using namespace playback_drop_off;
    DropOffRun run;
    const double periodMs = 1000.0 / kNativeFps;
    int target = 0, nextRender = 0, rendering = -1, computing = -1, presenting = -1, lastPresented = -1;
    double renderUntil = 0.0, prepUntil = 0.0, presentUntil = 0.0, lastAdvanceMs = -1.0e9;
    bool targetTaken = false;
    std::set<int> ready;
    std::deque<int> pending, results;
    AdvanceHold hold;
    double now = 0.0;
    for( ; now < 120000.0; now += kStepMs )
    {
        if( rendering >= 0 && now >= renderUntil ) { ready.insert( rendering ); rendering = -1; }
        if( rendering < 0 && nextRender < frames && nextRender <= target + 1 )
        {
            rendering = nextRender++;
            renderUntil = now + renderMs;
        }
        if( computing >= 0 && now >= prepUntil )
        {
            if( playbackPrepConflatesOlderWork( r3Policy ) ) results.clear();
            results.push_back( computing );
            computing = -1;
        }
        if( computing < 0 && !pending.empty() )
        {
            computing = pending.front();
            pending.pop_front();
            prepUntil = now + prepMs;
        }
        if( presenting >= 0 && now >= presentUntil ) presenting = -1;
        if( presenting < 0 && !results.empty() )
        {
            presenting = results.front();
            results.pop_front();
            presentUntil = now + presentMs;
            if( presenting != lastPresented + 1 ) run.inOrder = false;
            lastPresented = presenting;
            ++run.presented;
        }
        if( !targetTaken && ready.count( target ) ) // drawFrameReady takes the target into prep
        {
            ready.erase( target );
            targetTaken = true;
            if( playbackPrepConflatesOlderWork( r3Policy ) ) pending.clear();
            pending.push_back( target );
        }
        const int backlog = static_cast<int>( pending.size() + results.size() ) + ( computing >= 0 ? 1 : 0 );
        run.maxPrepBacklog = std::max( run.maxPrepBacklog, backlog );
        if( target + 1 < frames && targetTaken && now - lastAdvanceMs >= periodMs )
        {
            if( !hold.hold( advanceHeld( r3Policy, backlog, false ), now ) )
            {
                ++target;
                targetTaken = false;
                lastAdvanceMs = now;
            }
        }
        if( target + 1 == frames && targetTaken && backlog == 0 && presenting < 0 ) break;
    }
    run.elapsedMs = now;
    run.holdTimeouts = hold.timeouts;
    return run;
}

} // namespace

TEST(PlaybackDropOffPrepPolicy, LosslessWorkIsNeverConflatedOrSkippedBySerial)
{
    ASSERT_TRUE( playbackPrepLosslessActive( true, false ) );
    ASSERT_FALSE( playbackPrepLosslessActive( true, true ) );   // drop-frame on keeps the latest-wins queue
    ASSERT_FALSE( playbackPrepLosslessActive( false, false ) ); // paused/scrub too
    ASSERT_FALSE( playbackPrepTaskShouldCompute( 41, 42, 7, 7 ) );        // unchanged default
    ASSERT_TRUE( playbackPrepTaskShouldCompute( 41, 42, 7, 7, true ) );   // an older drop-off frame still computes
    ASSERT_FALSE( playbackPrepTaskShouldCompute( 41, 42, 7, 8, true ) );  // a display change still drops it
    ASSERT_TRUE( playbackPrepConflatesOlderWork( false ) );
    ASSERT_FALSE( playbackPrepConflatesOlderWork( true ) );
}

TEST(PlaybackDropOffPrepPolicy, TheAdvanceHoldsForAFullPrepStageOrAnUntakenTargetAndNeverForever)
{
    using namespace playback_drop_off;
    ASSERT_FALSE( advanceHeld( false, 9, true ) );                 // drop-frame on: never held
    ASSERT_FALSE( advanceHeld( true, kPrepMaxInFlight - 1, false ) );
    ASSERT_TRUE( advanceHeld( true, kPrepMaxInFlight, false ) );
    ASSERT_TRUE( advanceHeld( true, 0, true ) );
    AdvanceHold hold;
    ASSERT_FALSE( hold.hold( false, 0.0 ) );
    ASSERT_TRUE( hold.hold( true, 10.0 ) );
    ASSERT_TRUE( hold.hold( true, 10.0 + kAdvanceHoldMaxMs - 1.0 ) );
    ASSERT_FALSE( hold.hold( true, 10.0 + kAdvanceHoldMaxMs ) );  // the liveness backstop lets one advance through
    ASSERT_EQ( hold.timeouts, 1u );
    ASSERT_TRUE( hold.hold( true, 10.0 + kAdvanceHoldMaxMs + 1.0 ) ); // and a new hold starts
    ASSERT_EQ( hold.holds, 2u );
    ASSERT_FALSE( hold.hold( false, 2000.0 ) );
    hold.resetCounters();
    ASSERT_EQ( hold.holds, 0u );
    ASSERT_EQ( hold.timeouts, 0u );
}

TEST(PlaybackDropOffPrepPolicy, DropFrameOffPresentsEveryFrameInOrderWhenRenderOutrunsPresent)
{
    // The r2 s2 shape: render 41 ms with one lookahead frame, prep 30 ms, present 60 ms (arm A presented ~81 ms apart).
    const int kFrames = 240;
    const DropOffRun lossy = simulateDropOffPlayback( false, kFrames, 41.0, 30.0, 60.0 );
    const DropOffRun fixed = simulateDropOffPlayback( true, kFrames, 41.0, 30.0, 60.0 );
    ASSERT_TRUE( lossy.presented < kFrames * 9 / 10 );   // the old conflation loses frames
    ASSERT_FALSE( lossy.inOrder );
    ASSERT_EQ( fixed.presented, kFrames );               // r3: every frame
    ASSERT_TRUE( fixed.inOrder );                        // in order
    ASSERT_TRUE( fixed.maxPrepBacklog <= playback_drop_off::kPrepMaxInFlight + 1 ); // the queue stays bounded
    ASSERT_EQ( fixed.holdTimeouts, 0u );                 // the backstop never fires on a healthy pipeline
    ASSERT_TRUE( fixed.elapsedMs < kFrames * 60.0 * 1.10 ); // and it runs at the present stage's own pace
    // When present keeps up, the hold costs nothing: native pace, every frame.
    const DropOffRun fast = simulateDropOffPlayback( true, kFrames, 30.0, 20.0, 25.0 );
    ASSERT_EQ( fast.presented, kFrames );
    ASSERT_TRUE( fast.inOrder );
    ASSERT_TRUE( fast.elapsedMs < kFrames * ( 1000.0 / kNativeFps ) * 1.05 );
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

TEST(PlaybackDecodeRenderOverlapWiring, TheLookaheadQueueAdmitsTheCpuRouteAndTheSummaryNamesEveryRefusal)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    ASSERT_FALSE( mw.isEmpty() );
    const int queueFn = mw.indexOf( QStringLiteral("void MainWindow::queuePlaybackLookaheadRequests(") );
    ASSERT_TRUE( queueFn > 0 );
    const int queueFnEnd = mw.indexOf( QStringLiteral("\nvoid MainWindow::"), queueFn + 10 );
    const QString queueBody = mw.mid( queueFn, queueFnEnd - queueFn );
    ASSERT_TRUE( queueBody.contains(
        QStringLiteral("if( !textureRouteAdmitsLookahead && !m_playbackLookaheadCpuRouteEligible ) return;") ) );
    // The decision is recomputed every drawFrame() request from the pure policy, and the CPU default reaches the depth.
    ASSERT_TRUE( mw.contains( QStringLiteral("playback_overlap::cpuRouteLookaheadDecision(") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("m_playbackLookaheadCpuRouteDefaultAdmitted );") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("MLVAPP_DISABLE_CPU_PLAYBACK_LOOKAHEAD") ) );
    for( const char *field : { "cpu_route_eligible=", "cpu_route_default_admitted=", "cpu_route_refusal_last=",
                               "cpu_route_refusal_counts=", "lookahead_dropped_ready=", "lookahead_pruned=" } )
    {
        ASSERT_TRUE( mw.contains( QString::fromLatin1( field ) ) );
    }
}

TEST(PlaybackDecodeRenderOverlapWiring, DropFrameOffMakesThePrepQueueLosslessAndHoldsTheAdvance)
{
    const QString mw = readRepoText( "platform/qt/MainWindow.cpp" );
    ASSERT_FALSE( mw.isEmpty() );
    const int gateFn = mw.indexOf( QStringLiteral("bool MainWindow::renderThreadBusyForPlaybackAdvance(") );
    ASSERT_TRUE( gateFn > 0 );
    const QString gateBody = mw.mid( gateFn, mw.indexOf( QStringLiteral("\n}\n"), gateFn ) - gateFn );
    ASSERT_TRUE( gateBody.contains( QStringLiteral("playback_drop_off::advanceHeld(") ) );
    ASSERT_TRUE( gateBody.contains( QStringLiteral("playbackPrepBacklog()") ) );
    ASSERT_TRUE( gateBody.contains( QStringLiteral("hasReadyPlaybackFrame(") ) );
    ASSERT_TRUE( gateBody.contains( QStringLiteral("m_playbackDropOffAdvanceHold.hold(") ) );
    // The poll asks the gate afresh with drop-frame off instead of trusting a cached "not busy".
    const int timerFn = mw.indexOf( QStringLiteral("void MainWindow::timerFrameEvent(") );
    ASSERT_TRUE( timerFn > 0 );
    const QString timerHead = mw.mid( timerFn, mw.indexOf( QStringLiteral("if( m_frameStillDrawing )"), timerFn ) - timerFn );
    ASSERT_TRUE( timerHead.contains(
        QStringLiteral("m_frameStillDrawing = renderThreadBusyForPlaybackAdvance( currentPlaybackAdvanceTarget() );") ) );
    // The task carries the mode; the enqueue and the publish conflate only lossy work; the worker never skips it.
    ASSERT_TRUE( mw.contains( QStringLiteral("task.losslessPresent =\n        playbackPrepLosslessActive(") ) );
    ASSERT_EQ( mw.count( QStringLiteral("playbackPrepConflatesOlderWork(") ), 2 );
    ASSERT_TRUE( mw.contains( QStringLiteral("activeGenerationBeforeCompute,\n                                            task.losslessPresent ) )") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("drop_off_advance_holds=") ) );
    ASSERT_TRUE( mw.contains( QStringLiteral("drop_off_hold_timeouts=") ) );
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
