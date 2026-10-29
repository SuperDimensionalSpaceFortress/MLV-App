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
