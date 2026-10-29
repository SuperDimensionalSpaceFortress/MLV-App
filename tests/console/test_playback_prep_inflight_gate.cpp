// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F2: a drain returns only after the prep worker has left its in-flight section.
#include "../common/minitest.h"
#include "../../platform/qt/PlaybackPrepInFlightGate.h"

#include <algorithm>
#include <atomic>
#include <chrono>
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
    std::atomic<bool> taken{ false };
    std::atomic<bool> left{ false };
    std::thread worker( [&]() {
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            gate.beginLocked( 11 );
        }
        taken.store( true );
        std::this_thread::sleep_for( std::chrono::milliseconds( 200 ) );
        std::lock_guard<std::mutex> lk( queueMutex );
        left.store( true );
        gate.endLocked();
    } );
    while( !taken.load() ) std::this_thread::yield();

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
            // The callback runs with the queue mutex released, so it may log without deadlock.
            std::unique_lock<std::mutex> probe( queueMutex, std::try_to_lock );
            lockFreeAtTimeout = probe.owns_lock();
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
