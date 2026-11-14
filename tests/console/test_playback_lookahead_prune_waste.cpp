// CPU-S1-PACE-POST-785-LEVER-1 r4: CPU-LOOKAHEAD-PRUNE-WASTE-1 and CPU-LOOKAHEAD-ENV-DEPTH-SLOT-BUDGET-TEST-1.
//
// Prune waste. A CPU-route frame cost ~56 ms against a 41.7 ms period at 23.976 fps, so the drop-frame engine advanced
// ~1.35 frames per target. The lookahead asked for requestedFrame + 1. Every advance of 2 skipped it, and
// prunePlaybackLookaheadOutsideForwardWindow threw it away: 125-152 of ~587 requests per r3 H leg (fable r3,
// overlap_summary lookahead_pruned). The rule now: lookahead k on the CPU route asks for
// floor( position + k x stride ), the frame the engine is predicted to present next (playback_lookahead_cpu).
// The texture route keeps requestedFrame + k and the depth window, byte for byte.
//
// Slot budget. MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=2 or 3 issues real CPU-route requests against 4 frame slots,
// one held by the #774 borrow while the prep worker reads it. The model below runs the render thread's slot rule
// (findFreeSlotLocked: a rendering, ready, presenting or non-idle slot is never handed out, and with none free the
// run loop waits on m_waitCondition) under the real offset and window functions. It shows the thread waits, never
// holds more than 4 slots, and never deadlocks: the presenting slot is released by the prep hand-off, never by the
// next render.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackLookaheadCpuRoute.h"

#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <set>
#include <vector>

using playback_lookahead_cpu::DropFrameStride;
using playback_lookahead_cpu::lookaheadOffsetFrames;
using playback_lookahead_cpu::lookaheadWindowFrames;

namespace
{

// Deterministic jitter in [-amplitude, +amplitude] (a 32-bit LCG), so the run is the same on every host.
struct Jitter
{
    uint32_t state = 12345u;
    double next( double amplitude )
    {
        state = state * 1664525u + 1013904223u;
        const double unit = static_cast<double>( state >> 8 ) / static_cast<double>( 1u << 24 ); // [0, 1)
        return ( unit * 2.0 - 1.0 ) * amplitude;
    }
};

struct PruneRun
{
    int targets = 0;        // distinct targets the engine asked for
    int issued = 0;         // lookahead requests issued
    int pruned = 0;         // ...dropped because the engine moved past them (the frame was skipped)
    int covered = 0;        // targets that an earlier lookahead had already requested
};

// The drop-frame engine advances `advance` +/- `jitterFrames` positions per target. At each new target the prune
// keeps [target, target + window] (MainWindow::playbackLookaheadPruneWindowFrames), then the lookahead issues
// requestedFrame + lookaheadOffsetFrames( k, ... ) for k = 1..depth (MainWindow::queuePlaybackLookaheadRequests).
PruneRun simulateDropFramePrune( bool cpuRoute, int depth, double advance, double jitterFrames, int targets )
{
    PruneRun run;
    DropFrameStride stride;
    Jitter jitter;
    std::set<int> outstanding;
    double position = 0.0;
    int lastTarget = -1;
    while( run.targets < targets )
    {
        position += advance + jitter.next( jitterFrames );
        const int target = static_cast<int>( std::floor( position ) );
        if( target == lastTarget ) continue;
        lastTarget = target;
        ++run.targets;
        if( outstanding.count( target ) ) ++run.covered;
        const int window = lookaheadWindowFrames( depth, cpuRoute );
        for( auto it = outstanding.begin(); it != outstanding.end(); )
        {
            if( *it < target || *it > target + window )
            {
                ++run.pruned;
                it = outstanding.erase( it );
            }
            else if( *it == target )
            {
                it = outstanding.erase( it ); // presented from the lookahead
            }
            else
            {
                ++it;
            }
        }
        stride.noteTarget( target, position );
        const double fraction = position - static_cast<double>( target );
        for( int k = 1; k <= depth; ++k )
        {
            const int frame = target + lookaheadOffsetFrames( k, cpuRoute, fraction, stride.strideFrames() );
            if( frame == target || outstanding.count( frame ) ) continue;
            outstanding.insert( frame );
            ++run.issued;
        }
    }
    return run;
}

} // namespace

// RED on 80c753d8 (and on this card's RED commit): lookahead k asked for requestedFrame + k and the prune kept + depth,
// so at 1.35 frames per target about a third of the requests were for frames the engine then skipped.
TEST(PlaybackLookaheadPruneWaste, ACpuLookaheadIsNotIssuedForAFrameTheDropFrameEngineSkips)
{
    const PruneRun run = simulateDropFramePrune( true, 1, 1.35, 0.10, 600 );
    std::printf( "prune_waste cpu depth=1 advance=1.35 targets=%d issued=%d pruned=%d covered=%d\n",
                 run.targets, run.issued, run.pruned, run.covered );
    ASSERT_TRUE( run.issued > 0 );
    ASSERT_TRUE( run.pruned * 10 <= run.issued );         // at most 10 % pruned (r3 legs: 21-26 %)
    ASSERT_TRUE( run.covered * 100 >= run.targets * 85 ); // the lookahead still covers at least 85 % of targets
}

TEST(PlaybackLookaheadPruneWaste, AtNativePaceTheCpuLookaheadIsStillTheNextFrame)
{
    // When frames keep pace the engine advances one frame per target: the rule must not jump ahead.
    const PruneRun run = simulateDropFramePrune( true, 1, 1.0, 0.0, 400 );
    ASSERT_EQ( run.pruned, 0 );
    ASSERT_EQ( run.covered, run.targets - 1 );
    ASSERT_EQ( lookaheadOffsetFrames( 1, true, 0.0, 1.0 ), 1 );
    ASSERT_EQ( lookaheadOffsetFrames( 2, true, 0.0, 1.0 ), 2 );
}

TEST(PlaybackLookaheadPruneWaste, TheTextureRouteOffsetAndWindowAreUnchanged)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    const double fractions[] = { 0.0, 0.3, 0.65, 0.99, -0.5, 1.5, nan, inf };
    const double strides[] = { 0.0, 1.0, 1.35, 2.0, 2.99, 3.0, 9.0, nan, -inf };
    for( int k = -1; k <= 5; ++k )
        for( double f : fractions )
            for( double s : strides )
                ASSERT_EQ( lookaheadOffsetFrames( k, false, f, s ), k );
    for( int depth = -1; depth <= 5; ++depth )
        ASSERT_EQ( lookaheadWindowFrames( depth, false ), depth );
    // And the texture route still prunes exactly as before under the same engine.
    const PruneRun texture = simulateDropFramePrune( false, 2, 1.0, 0.0, 200 );
    ASSERT_EQ( texture.pruned, 0 );
}

TEST(PlaybackLookaheadPruneWaste, TheCpuOffsetIsAtLeastKIncreasesWithKAndStaysInsideThePruneWindow)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double fractions[] = { 0.0, 0.2, 0.5, 0.65, 0.999, -0.1, 1.0, nan };
    const double strides[] = { -1.0, 0.5, 1.0, 1.2, 1.35, 1.5, 2.0, 2.7, 3.0, 4.0, 100.0, nan };
    for( int depth = 1; depth <= 3; ++depth )
    {
        for( double f : fractions )
        {
            for( double s : strides )
            {
                int previous = 0;
                for( int k = 1; k <= depth; ++k )
                {
                    const int offset = lookaheadOffsetFrames( k, true, f, s );
                    ASSERT_TRUE( offset >= k );
                    ASSERT_TRUE( offset > previous );
                    ASSERT_TRUE( offset <= lookaheadWindowFrames( depth, true ) );
                    previous = offset;
                }
            }
        }
    }
    ASSERT_EQ( lookaheadOffsetFrames( 1, true, 0.70, 1.35 ), 2 ); // 0.70 + 1.35 = 2.05: the engine skips +1
    ASSERT_EQ( lookaheadOffsetFrames( 1, true, 0.60, 1.35 ), 1 ); // 0.60 + 1.35 = 1.95: it presents +1
    ASSERT_EQ( lookaheadOffsetFrames( 0, true, 0.70, 1.35 ), 0 );
}

TEST(PlaybackLookaheadPruneWaste, TheStrideSamplesDistinctTargetsAndIgnoresRepeatsWrapsAndNonFinitePositions)
{
    DropFrameStride stride;
    ASSERT_NEAR( stride.strideFrames(), 1.0, 1e-12 );
    stride.noteTarget( 10, 10.2 );
    ASSERT_NEAR( stride.strideFrames(), 1.0, 1e-12 );      // the first target is not an advance
    stride.noteTarget( 12, 12.0 );                         // +1.8
    ASSERT_NEAR( stride.strideFrames(), 1.0 + 0.25 * 0.8, 1e-12 );
    const double afterOne = stride.strideFrames();
    stride.noteTarget( 12, 12.9 );                         // a repeat: no sample
    ASSERT_NEAR( stride.strideFrames(), afterOne, 1e-12 );
    stride.noteTarget( 3, 3.1 );                           // a Loop wrap: no sample, restart from here
    ASSERT_NEAR( stride.strideFrames(), afterOne, 1e-12 );
    stride.noteTarget( 4, std::numeric_limits<double>::quiet_NaN() );
    ASSERT_NEAR( stride.strideFrames(), afterOne, 1e-12 );
    stride.noteTarget( 50, 50.0 );                         // after a NaN the position restarted at 0: capped at 3
    ASSERT_NEAR( stride.strideFrames(), afterOne + 0.25 * ( 3.0 - afterOne ), 1e-12 );
    stride.reset();
    ASSERT_NEAR( stride.strideFrames(), 1.0, 1e-12 );
    for( int i = 0; i < 200; ++i ) stride.noteTarget( i * 2, i * 1.35 + 0.0 ); // converges to the position advance
    ASSERT_NEAR( stride.strideFrames(), 1.35, 1e-6 );
}

namespace
{

enum class SlotState { Idle, Busy, Ready, Presenting };

struct ModelSlot
{
    SlotState state = SlotState::Idle;
    int frame = -1;
    bool lookahead = false;
    double doneMs = 0.0;
};

struct SlotBudgetRun
{
    int presented = 0;
    int maxSlotsInUse = 0;
    int waitsForFreeSlot = 0;       // steps the render thread had a request and no free slot
    double maxPresentGapMs = 0.0;
    bool slotOverrun = false;
};

// One CPU-route playback against kSlots frame slots. The render pipeline takes the front request only when
// findFreeSlot() >= 0 (else it waits), works pipelineMs, and marks the slot Ready. drawFrameReady presents the ready
// slot of the target: the slot is Presenting (the #774 borrow) until the prep worker hands it back prepMs later.
// The drop-frame engine moves on wall time once the target is presented; each new target prunes outside
// [target, target + window], issues the target if no request or slot covers it (erasing queued lookaheads, as the
// drop-frame request does at RenderFrameThread.cpp:1240-1257), then the lookahead at the real offsets.
SlotBudgetRun simulateSlotBudget( int depth, double pipelineMs, double prepMs, double wallMs )
{
    constexpr int kSlots = 4;      // RenderFrameThread::kDefaultFrameSlotCount
    constexpr double kStepMs = 0.5;
    constexpr double kFps = 24000.0 / 1001.0;
    SlotBudgetRun run;
    std::vector<ModelSlot> slots( kSlots );
    struct Request { int frame; bool lookahead; };
    std::deque<Request> queue;
    DropFrameStride stride;
    int target = -1;
    bool targetPresented = true;
    double lastPresentMs = 0.0;
    const int window = lookaheadWindowFrames( depth, true );

    const auto findFreeSlot = [&]()
    {
        for( int i = 0; i < kSlots; ++i ) if( slots[i].state == SlotState::Idle ) return i;
        return -1;
    };
    const auto covered = [&]( int frame )
    {
        for( const Request &r : queue ) if( r.frame == frame ) return true;
        for( const ModelSlot &s : slots )
            if( s.frame == frame && ( s.state == SlotState::Busy || s.state == SlotState::Ready ) ) return true;
        return false;
    };

    for( double now = 0.0; now <= wallMs; now += kStepMs )
    {
        // Pipeline completions, then the prep hand-off that releases the presenting slot.
        for( ModelSlot &s : slots )
        {
            if( s.state == SlotState::Busy && now >= s.doneMs ) s.state = SlotState::Ready;
            else if( s.state == SlotState::Presenting && now >= s.doneMs ) s = ModelSlot();
        }
        // The engine: a new target once the last one is presented.
        if( targetPresented )
        {
            const double position = now * kFps / 1000.0;
            const int next = static_cast<int>( std::floor( position ) );
            if( next != target )
            {
                target = next;
                targetPresented = false;
                stride.noteTarget( target, position );
                for( ModelSlot &s : slots )
                    if( s.state == SlotState::Ready && s.lookahead && s.frame != target
                     && ( s.frame < target || s.frame > target + window ) )
                        s = ModelSlot();
                for( auto it = queue.begin(); it != queue.end(); )
                    if( it->lookahead && ( it->frame < target || it->frame > target + window ) ) it = queue.erase( it );
                    else ++it;
                if( !covered( target ) )
                {
                    for( auto it = queue.begin(); it != queue.end(); )
                        if( it->lookahead ) it = queue.erase( it ); else ++it;
                    queue.push_back( { target, false } );
                }
                const double fraction = position - static_cast<double>( target );
                for( int k = 1; k <= depth; ++k )
                {
                    const int frame = target + lookaheadOffsetFrames( k, true, fraction, stride.strideFrames() );
                    if( !covered( frame ) ) queue.push_back( { frame, true } );
                }
            }
        }
        // The render thread: one request per free slot, or wait (findFreeSlotLocked() < 0).
        while( !queue.empty() )
        {
            const int slot = findFreeSlot();
            if( slot < 0 ) { ++run.waitsForFreeSlot; break; }
            slots[slot].state = SlotState::Busy;
            slots[slot].frame = queue.front().frame;
            slots[slot].lookahead = queue.front().lookahead;
            slots[slot].doneMs = now + pipelineMs;
            queue.pop_front();
            break; // one pipeline: the next request waits for the next step
        }
        // drawFrameReady: present the target's ready slot once the prep worker is free.
        bool prepBusy = false;
        for( const ModelSlot &s : slots ) if( s.state == SlotState::Presenting ) prepBusy = true;
        if( !targetPresented && !prepBusy )
        {
            for( ModelSlot &s : slots )
            {
                if( s.state == SlotState::Ready && s.frame == target )
                {
                    s.state = SlotState::Presenting;
                    s.doneMs = now + prepMs;
                    targetPresented = true;
                    if( run.presented > 0 && now - lastPresentMs > run.maxPresentGapMs )
                        run.maxPresentGapMs = now - lastPresentMs;
                    lastPresentMs = now;
                    ++run.presented;
                    break;
                }
            }
        }
        int inUse = 0;
        for( const ModelSlot &s : slots ) if( s.state != SlotState::Idle ) ++inUse;
        if( inUse > kSlots ) run.slotOverrun = true;
        if( inUse > run.maxSlotsInUse ) run.maxSlotsInUse = inUse;
    }
    if( wallMs - lastPresentMs > run.maxPresentGapMs ) run.maxPresentGapMs = wallMs - lastPresentMs;
    return run;
}

} // namespace

TEST(PlaybackLookaheadCpuSlotBudget, EnvDepthTwoAndThreeWaitForAFreeSlotAndNeverDeadlockOrOverrun)
{
    for( int depth = 2; depth <= 3; ++depth )
    {
        // Slow (the r3 CPU cost: a frame every ~56 ms) and fast (keeps pace) pipelines.
        const double pipelines[] = { 56.0, 30.0 };
        for( double pipelineMs : pipelines )
        {
            const SlotBudgetRun run = simulateSlotBudget( depth, pipelineMs, 36.0, 20000.0 );
            std::printf( "slot_budget depth=%d pipeline=%.0f presented=%d max_slots=%d waits=%d max_gap_ms=%.1f\n",
                         depth, pipelineMs, run.presented, run.maxSlotsInUse, run.waitsForFreeSlot,
                         run.maxPresentGapMs );
            ASSERT_FALSE( run.slotOverrun );
            ASSERT_TRUE( run.maxSlotsInUse <= 4 );
            ASSERT_TRUE( run.presented >= 200 );          // 20 s: no deadlock, playback keeps presenting
            ASSERT_TRUE( run.maxPresentGapMs < 500.0 );   // and never stalls for a FREEZE-sized gap
        }
    }
    // At depth 3 the target, three lookaheads and the borrowed presenting slot want 5 slots: the thread waits.
    const SlotBudgetRun deep = simulateSlotBudget( 3, 30.0, 36.0, 20000.0 );
    ASSERT_TRUE( deep.waitsForFreeSlot > 0 );
    ASSERT_EQ( deep.maxSlotsInUse, 4 );
}
