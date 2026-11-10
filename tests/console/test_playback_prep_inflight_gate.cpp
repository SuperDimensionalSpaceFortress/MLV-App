// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F2: a drain returns only after the prep worker has left its in-flight section.
#include "../common/minitest.h"
#include "../../platform/qt/PlaybackPrepInFlightGate.h"
#include "../../platform/qt/PlaybackFrameRange.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <future>
#include <mutex>
#include <thread>
#include <vector>

TEST(PlaybackPrepInFlightGate, DrainReturnsOnlyAfterWorkerLeavesInFlightSection)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::atomic<bool> left{ false };
    // r2 (CoS I1): handshakes, not a sleep, hold the worker in flight until the
    // observer has seen it there, so a delayed observer never meets a finished worker.
    std::promise<void> taken;
    std::promise<void> release;
    std::future<void> takenSignal = taken.get_future();
    std::shared_future<void> releaseSignal = release.get_future().share();
    std::thread worker( [&]() {
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            gate.beginLocked( 42 );
        }
        taken.set_value();
        releaseSignal.wait();
        std::lock_guard<std::mutex> lk( queueMutex );
        left.store( true );
        gate.endLocked();
    } );
    takenSignal.wait();

    bool sawInFlight = false;
    uint64_t serial = 0;
    bool drained = false;
    {
        std::unique_lock<std::mutex> lk( queueMutex );
        sawInFlight = gate.inFlightLocked();
        serial = gate.serialLocked();
        // The worker may leave only now, and only through the mutex drainLocked releases.
        release.set_value();
        drained = gate.drainLocked( lk, 2000 );
    }
    const bool leftWhenDrainReturned = left.load();
    worker.join();

    ASSERT_TRUE( sawInFlight );
    ASSERT_EQ( static_cast<uint64_t>( 42 ), serial );
    ASSERT_TRUE( drained );
    ASSERT_TRUE( leftWhenDrainReturned );
}

TEST(PlaybackPrepInFlightGate, DrainTimesOutInsteadOfHanging)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::unique_lock<std::mutex> lk( queueMutex );
    gate.beginLocked( 7 );
    const auto start = std::chrono::steady_clock::now();
    const bool drained = gate.drainLocked( lk, 40 );
    const auto waited = std::chrono::steady_clock::now() - start;
    ASSERT_FALSE( drained );
    ASSERT_TRUE( waited >= std::chrono::milliseconds( 30 ) );
    ASSERT_TRUE( gate.inFlightLocked() );
}

// r9 (adversary A1): a core mutation's drain that outlives its timeout must still not return
// while the worker is in flight -- MainWindow mutates the processing state right after it.
TEST(PlaybackPrepInFlightGate, CoreMutationDrainNeverReturnsWhileWorkerIsInFlight)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::atomic<bool> left{ false };
    // r13 (adversary A4): the promise latch master's tests use, not a sleep, holds the worker in
    // flight; only the timeout callback releases it, so the bounded wait always runs out first.
    std::promise<void> taken;
    std::promise<void> release;
    std::future<void> takenSignal = taken.get_future();
    std::shared_future<void> releaseSignal = release.get_future().share();
    std::thread worker( [&]() {
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            gate.beginLocked( 11 );
        }
        taken.set_value();
        releaseSignal.wait();
        std::lock_guard<std::mutex> lk( queueMutex );
        left.store( true );
        gate.endLocked();
    } );
    takenSignal.wait();

    int timeouts = 0;
    bool workerInFlightAtTimeout = false;
    bool lockFreeAtTimeout = false;
    bool drainedInTime = true;
    bool inFlightAfter = true;
    {
        std::unique_lock<std::mutex> lk( queueMutex );
        drainedInTime = gate.drainForCoreMutationLocked( lk, 20, [&]() {
            ++timeouts;
            workerInFlightAtTimeout = !left.load();
            {
                // The callback runs with the queue mutex released, so it may log without deadlock.
                std::unique_lock<std::mutex> probe( queueMutex, std::try_to_lock );
                lockFreeAtTimeout = probe.owns_lock();
            }
            // The worker may leave only now; the drain must still wait for it.
            release.set_value();
        } );
        inFlightAfter = gate.inFlightLocked();
    }
    const bool leftWhenDrainReturned = left.load();
    worker.join();

    ASSERT_FALSE( drainedInTime );
    ASSERT_EQ( 1, timeouts );
    ASSERT_TRUE( workerInFlightAtTimeout );
    ASSERT_TRUE( lockFreeAtTimeout );
    ASSERT_TRUE( leftWhenDrainReturned );
    ASSERT_FALSE( inFlightAfter );
}

TEST(PlaybackPrepInFlightGate, CoreMutationDrainWithinTimeoutNeverCallsOnTimeout)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::unique_lock<std::mutex> lk( queueMutex );
    gate.beginLocked( 5 );
    gate.endLocked();
    int timeouts = 0;
    ASSERT_TRUE( gate.drainForCoreMutationLocked( lk, 2000, [&]() { ++timeouts; } ) );
    ASSERT_EQ( 0, timeouts );
    ASSERT_TRUE( lk.owns_lock() );
}

TEST(PlaybackPrepInFlightGate, DrainWithNothingInFlightReturnsAtOnce)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::unique_lock<std::mutex> lk( queueMutex );
    gate.beginLocked( 3 );
    gate.endLocked();
    ASSERT_FALSE( gate.inFlightLocked() );
    ASSERT_TRUE( gate.drainLocked( lk, 2000 ) );
}

// CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 r2 (CoS B1): a clip replacement after a drain
// that ran out of budget. The task is held on a latch, so the drain must time out; the
// replacement (here: the poison setMlvProcessing's realloc/free leaves behind in the
// gradient mask the task borrows) must not run until the task has left.
TEST(PlaybackPrepInFlightGate, BorrowFreeingMutationNeverRunsAfterATimedOutDrain)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::vector<uint16_t> gradientMask( 256, 0x1234 );  // processing->gradient_mask
    const uint16_t *borrowed = gradientMask.data();      // the task's config.gradientMaskData
    std::promise<void> taken;
    std::promise<void> release;
    std::future<void> takenSignal = taken.get_future();
    std::shared_future<void> releaseSignal = release.get_future().share();
    bool taskReadIntactMask = false;
    std::thread worker( [&]() {
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            gate.beginLocked( 11 );
        }
        taken.set_value();
        releaseSignal.wait();
        bool intact = true;  // the task's last read of the borrowed mask
        for( size_t i = 0; i < gradientMask.size(); ++i ) intact = intact && borrowed[i] == 0x1234;
        std::lock_guard<std::mutex> lk( queueMutex );
        taskReadIntactMask = intact;
        gate.endLocked();
    } );
    takenSignal.wait();

    int replacements = 0;
    const auto replaceClipIfAllowed = [&]( const PlaybackPrepDrainOutcome &outcome ) {
        if( !playbackPrepBorrowFreeingMutationAllowed( outcome ) ) return false;
        std::fill( gradientMask.begin(), gradientMask.end(), static_cast<uint16_t>( 0xDEAD ) );
        ++replacements;
        return true;
    };

    // The latch, not this budget, guarantees the task is still in flight here.
    const PlaybackPrepDrainOutcome timedOut = playbackPrepDrainInFlight( queueMutex, gate, 20 );
    const bool replacedWhileHeld = replaceClipIfAllowed( timedOut );

    release.set_value();
    worker.join();
    const PlaybackPrepDrainOutcome afterTask = playbackPrepDrainInFlight( queueMutex, gate, 2000 );
    const bool replacedAfterTask = replaceClipIfAllowed( afterTask );

    ASSERT_TRUE( timedOut.waited );
    ASSERT_FALSE( timedOut.drained );
    ASSERT_EQ( static_cast<uint64_t>( 11 ), timedOut.serial );
    ASSERT_FALSE( replacedWhileHeld );
    ASSERT_TRUE( taskReadIntactMask );
    ASSERT_FALSE( afterTask.waited );
    ASSERT_TRUE( afterTask.drained );
    ASSERT_TRUE( replacedAfterTask );
    ASSERT_EQ( 1, replacements );
}

TEST(PlaybackPrepInFlightGate, DrainOutcomeWithNothingInFlightAllowsTheMutation)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    const PlaybackPrepDrainOutcome outcome = playbackPrepDrainInFlight( queueMutex, gate, 0 );
    ASSERT_FALSE( outcome.waited );
    ASSERT_TRUE( outcome.drained );
    ASSERT_TRUE( playbackPrepBorrowFreeingMutationAllowed( outcome ) );
}

// HFR-NATIVE-60FPS-FULLRES-1 r28 (CoS B1 6714996522 on c4efe7e3): Play / conform / Drop Frame, Loop off, NON-INLINE CPU prep.
// Frame 100 is on screen; drawFrameReady accepted 101 (m_lastPresentedPlaybackFrame = 101) BEFORE it was displayed, and the
// engine sits at 101.992 with a debt of 1. On c4efe7e3 the accepted 101 opened the gate, so the 0.192 grant repaid to 102.184,
// 102 was requested, and its enqueue could replace the still-pending prep of 101 (enqueuePlaybackPrepTask): 100 -> 102 on screen,
// below the debt cap. The gate now keys off the frame DISPLAYED, and a frame the prep queue still holds is a present to come.
// A real worker thread, held at barriers, walks the queue the way playbackPrepThreadLoop does: (i) the prep pending (not
// picked up), (iii) in flight, (ii) completed but not consumed -- then consumed, and only then does the engine move on.
TEST(PlaybackPrepInFlightGate, R28B1AnAcceptedFrameTheNonInlinePrepStillHoldsNeverOpensTheConformGate)
{
    std::mutex queueMutex;
    std::condition_variable queueCv;
    PlaybackPrepInFlightGate gate;
    bool pendingValid = false;
    int64_t pendingFrame = -1;
    uint64_t pendingSerial = 0;
    std::deque<int64_t> results;   // completed results' display frames
    const auto frameOf = []( int64_t frame ) { return frame; };

    const long long accepted = 101;
    long long displayed = 100;
    // the engine's next tick from 101.992 (debt 1, grant 0.192): the frame it lands on
    const auto tickLandsOn = [&]( bool acceptedPresentPending ) -> int
    {
        double debt = 1.0;
        const bool shown = playback_frame_range::conformTargetShown( accepted, displayed, 101.992, acceptedPresentPending, false );
        const double delta = playback_frame_range::conformDropFrameDelta( 101.992, 0.192, true, debt, shown );
        return static_cast<int>( std::floor( 101.992 + delta ) );
    };
    const auto heldLocked = [&]() {
        return playbackPrepHoldsFrameLocked( pendingValid, pendingFrame, gate, results, frameOf, accepted );
    };

    // drawFrameReady's own window: 101 accepted, its present not dispatched yet (the early advance runs here)
    ASSERT_EQ( 101, tickLandsOn( true ) );

    std::promise<void> pickUp, taken, computeDone, completed;
    std::shared_future<void> pickUpSignal = pickUp.get_future().share();
    std::shared_future<void> computeDoneSignal = computeDone.get_future().share();
    std::future<void> takenSignal = taken.get_future();
    std::future<void> completedSignal = completed.get_future();
    std::thread worker( [&]() {
        pickUpSignal.wait();   // barrier: the task stays pending until the observer has seen it there
        int64_t frame = -1;
        {
            std::unique_lock<std::mutex> lk( queueMutex );
            queueCv.wait( lk, [&]{ return pendingValid; } );
            frame = pendingFrame;
            pendingValid = false;
            gate.beginLocked( pendingSerial, frame );
        }
        taken.set_value();
        computeDoneSignal.wait();   // barrier: in flight until released
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            results.push_back( frame );
            gate.endLocked();
        }
        completed.set_value();
    } );

    // enqueuePlaybackPrepTask( 101 )
    {
        std::lock_guard<std::mutex> lk( queueMutex );
        pendingFrame = accepted;
        pendingSerial = 7;
        pendingValid = true;
    }
    queueCv.notify_one();
    // (i) pending, not picked up
    bool heldPending = false;
    { std::lock_guard<std::mutex> lk( queueMutex ); heldPending = heldLocked(); }
    ASSERT_TRUE( heldPending );
    ASSERT_EQ( 101, tickLandsOn( heldPending ) );
    // (iii) in flight
    pickUp.set_value();
    takenSignal.wait();
    bool heldInFlight = false;
    { std::lock_guard<std::mutex> lk( queueMutex ); heldInFlight = heldLocked(); ASSERT_FALSE( pendingValid ); }
    ASSERT_TRUE( heldInFlight );
    ASSERT_EQ( 101, tickLandsOn( heldInFlight ) );
    // (ii) completed, not consumed (onPlaybackPrepResultReady has not run: queued signal, or Look Assist holding it)
    computeDone.set_value();
    completedSignal.wait();
    worker.join();
    bool heldUnconsumed = false;
    { std::lock_guard<std::mutex> lk( queueMutex ); heldUnconsumed = heldLocked(); ASSERT_FALSE( gate.inFlightLocked() ); }
    ASSERT_TRUE( heldUnconsumed );
    ASSERT_EQ( 101, tickLandsOn( heldUnconsumed ) );
    // consumed and displayed (presentPlaybackPreparedFrame -> finishPresentedFrame): the debt is repaid, one frame on
    {
        std::lock_guard<std::mutex> lk( queueMutex );
        ASSERT_EQ( 101, static_cast<int>( results.front() ) );
        results.pop_front();
        ASSERT_FALSE( heldLocked() );
    }
    displayed = 101;
    ASSERT_EQ( 102, tickLandsOn( false ) );

    // the escape is kept: 101 accepted, its prep gone without a display (dropped), the renderer idle -- nothing is coming, so
    // nothing holds (adversary A1 on d98be193)
    displayed = 100;
    ASSERT_EQ( 102, tickLandsOn( false ) );
    // and a target the renderer still owes, not yet accepted, holds as before (r27)
    ASSERT_FALSE( playback_frame_range::conformTargetShown( 100, 100, 101.992, false, true ) );
    ASSERT_TRUE( playback_frame_range::conformTargetShown( 100, 100, 101.992, false, false ) );
    // a displayed target is shown whatever is pending
    ASSERT_TRUE( playback_frame_range::conformTargetShown( 101, 101, 101.992, true, true ) );
}

// r28: drawFrameReady's dispatch window names the accepted frame until the call returns, and a nested call gives the outer
// window's frame back
TEST(PlaybackPrepInFlightGate, R28B1ThePresentDispatchWindowNamesTheAcceptedFrameUntilTheCallReturns)
{
    long long slot = -1;
    {
        PlaybackPresentDispatchWindow outer( slot, 101 );
        ASSERT_EQ( 101LL, slot );
        {
            PlaybackPresentDispatchWindow nested( slot, 102 );
            ASSERT_EQ( 102LL, slot );
        }
        ASSERT_EQ( 101LL, slot );
    }
    ASSERT_EQ( -1LL, slot );
}

// HFR-NATIVE-60FPS-FULLRES-1 r28 fix iteration 1 (adversary A1 on e052947c), its literal input: Play / conform / Drop Frame, Loop off;
// painted 100, accepted 101, position 101.992, debt 1, grant 0.192; 101 submitted to the deferred GL viewport, renderer idle.
// finishPresentedFrame recorded 101 as displayed and its tail tick (the same stack) moved to 102.184 while the surface still showed 100.
// Displayed now means painted: while the surface holds 101's serial unpainted, the gate gets no displayed frame and the accepted 101
// is a present still to come -- bounded, so a surface that never paints holds nothing past kConformPaintWaitMs.
TEST(PlaybackPrepInFlightGate, R28A1aFrameHandedToADeferredGlSurfaceIsDisplayedOnlyOncePainted)
{
    const auto tickLandsOn = []( long long displayedInput, bool acceptedPending ) -> int
    {
        double debt = 1.0;
        const bool shown = playback_frame_range::conformTargetShown( 101, displayedInput, 101.992, acceptedPending, false );
        return static_cast<int>( std::floor( 101.992 + playback_frame_range::conformDropFrameDelta( 101.992, 0.192, true, debt, shown ) ) );
    };
    // the surface was handed 101 and has not painted it, 2 ms after the hand-over (the tail tick's own stack)
    const bool pending = playback_frame_range::conformDisplayPaintPending( true, 2.0 );
    ASSERT_TRUE( pending );
    ASSERT_EQ( 101, tickLandsOn( pending ? -1 : 101, pending ) );
    // painted: the debt is repaid, one frame on
    ASSERT_FALSE( playback_frame_range::conformDisplayPaintPending( false, 9.0 ) );
    ASSERT_EQ( 102, tickLandsOn( 101, false ) );
    // a surface that does not paint (hidden, not exposed) holds nothing past the bound
    ASSERT_TRUE( playback_frame_range::conformDisplayPaintPending( true, playback_frame_range::kConformPaintWaitMs - 1.0 ) );
    ASSERT_FALSE( playback_frame_range::conformDisplayPaintPending( true, playback_frame_range::kConformPaintWaitMs ) );
    ASSERT_EQ( 102, tickLandsOn( 101, false ) );
}