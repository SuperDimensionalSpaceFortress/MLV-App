// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F2: a drain returns only after the prep worker has left its in-flight section.
#include "../common/minitest.h"
#include "../../platform/qt/PlaybackPrepInFlightGate.h"

#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>

TEST(PlaybackPrepInFlightGate, DrainReturnsOnlyAfterWorkerLeavesInFlightSection)
{
    std::mutex queueMutex;
    PlaybackPrepInFlightGate gate;
    std::atomic<bool> taken{ false };
    std::atomic<bool> left{ false };
    std::thread worker( [&]() {
        {
            std::lock_guard<std::mutex> lk( queueMutex );
            gate.beginLocked( 42 );
        }
        taken.store( true );
        std::this_thread::sleep_for( std::chrono::milliseconds( 60 ) );
        std::lock_guard<std::mutex> lk( queueMutex );
        left.store( true );
        gate.endLocked();
    } );
    while( !taken.load() ) std::this_thread::yield();

    bool sawInFlight = false;
    uint64_t serial = 0;
    bool drained = false;
    {
        std::unique_lock<std::mutex> lk( queueMutex );
        sawInFlight = gate.inFlightLocked();
        serial = gate.serialLocked();
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
