// PLAYBACK-LJ92-DECODE-THROUGHPUT-1: K raw-uint16 prefetch decoders of different frames under one claim discipline.
// One LJ92 frame cannot be split (a single scan, no restart markers), so throughput comes from decoding different
// frames at once. Every decoder (worker or foreground reader) records a claim for (frame, generation) before it
// decodes, and nobody starts a decode of a frame that is READY or already claimed. These tests park decoders at their
// hold points to drive the races deterministically, and count getMlvRawFrameUint16Direct calls per frame.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include <QString>

#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <thread>
#include <vector>

namespace
{

// The test-only knobs are process-wide; restore them even when an assertion returns early. Declare it AFTER the
// fixture so it is destroyed first: a parked worker must be released before freeMlvObject joins it.
struct PrefetchKnobs
{
    explicit PrefetchKnobs( int decoders ) { mlvSetRawUint16PrefetchDecodersForTesting( decoders ); }
    ~PrefetchKnobs()
    {
        mlvSetRawUint16ForegroundHoldBeforeDecodeForTesting( 0 );
        mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
        mlvResetRawUint16DecodeCountsForTesting( 0 );
        mlvSetPlaybackAggressivePreviewMode( 0 );
        mlvSetRawUint16PrefetchDecodersForTesting( -1 );
    }
};

// Releases every hold before joining, so an early return never leaves a parked reader behind.
struct ReaderThread
{
    std::thread thread;
    ~ReaderThread()
    {
        mlvSetRawUint16ForegroundHoldBeforeDecodeForTesting( 0 );
        mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
        if( thread.joinable() ) thread.join();
    }
};

mlvObject_t *openLargeDualIso( MlvPipelineFixture *fixture )
{
    mlvSetPlaybackAggressivePreviewMode( 0 );
    processingSetPlaybackPreviewMode( 0 );
    QString error_message;
    if( !fixture->openClipFile( repo_file_path( QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv") ), &error_message ) )
        return nullptr;
    mlvObject_t *video = fixture->video();
    video->playback_scale_factor_active = 1;
    // Opening the clip already reads frames through the prefetch path; start every test from an idle, empty ring.
    if( !mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) ) return nullptr;
    mlvCancelPreviewPrefetch( video );
    mlvResetRawUint16PrefetchStats( video );
    return video;
}

bool waitFor( const std::function<bool()> &condition, int timeoutMs )
{
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds( timeoutMs );
    while( !condition() )
    {
        if( std::chrono::steady_clock::now() >= deadline ) return false;
        std::this_thread::sleep_for( std::chrono::milliseconds( 1 ) );
    }
    return true;
}

size_t frameWords( const MlvPipelineFixture &fixture )
{
    return static_cast<size_t>( fixture.width() ) * static_cast<size_t>( fixture.height() );
}

mlvRawUint16PrefetchStats_t prefetchStats( mlvObject_t *video )
{
    mlvRawUint16PrefetchStats_t stats;
    getMlvRawUint16PrefetchStats( video, &stats );
    return stats;
}

const int kDropPattern[] = { 1, 2, 1, 3 };

} // namespace

// One request with two decoders: both park on their own claims, base+1 and base+2.
TEST(RawUint16PrefetchParallel, TwoDecodersClaimDifferentFrames)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 2 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );
    ASSERT_EQ( 2u, mlvRawUint16PrefetchLookaheadForTesting( video ) );

    std::vector<uint16_t> frame( frameWords( fixture ) );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 1 );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 4, frame.data() ) );   // the window is 5, 6
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchHeldBeforeDecodeForTesting( 10000 ) );
    waitFor( [] { return mlvRawUint16PrefetchHeldCountForTesting() >= 2; }, 2000 );
    const int held = mlvRawUint16PrefetchHeldCountForTesting();
    uint64_t claimed[MLV_RAW_UINT16_PREFETCH_SLOTS] = {};
    const int claims = mlvRawUint16PrefetchClaimsForTesting( video, 0, claimed, MLV_RAW_UINT16_PREFETCH_SLOTS );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );

    ASSERT_EQ( 2, held );
    ASSERT_EQ( 2, claims );
    ASSERT_EQ( 5u, claimed[0] );
    ASSERT_EQ( 6u, claimed[1] );
    ASSERT_EQ( 0u, prefetchStats( video ).duplicate_publishes );
    mlvCancelPreviewPrefetch( video );
}

// The hole PR #264 left open: a foreground miss decoded with no claim, and the worker, which only skipped READY
// frames, decoded the same frame again. The reader misses frame 7 while the workers are parked on 5 (and 6), and
// parks right before its own decode; then the workers run the rest of the window and go idle.
TEST(RawUint16PrefetchParallel, NoFrameIsDecodedTwiceWhenTheForegroundMissesAFrameTheWorkerWouldTake)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 2 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    // A 4-frame window, so frame 7 is in it and not claimed yet. The aggressive flag is process-wide; the preview-mode
    // flag is thread-local and the worker would not see it.
    mlvSetPlaybackAggressivePreviewMode( 1 );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );
    ASSERT_EQ( 4u, mlvRawUint16PrefetchLookaheadForTesting( video ) );
    const uint64_t frames = getMlvFrames( video );
    ASSERT_TRUE( frames > 12 );

    const size_t words = frameWords( fixture );
    std::vector<uint16_t> frame( words );
    mlvResetRawUint16DecodeCountsForTesting( 1 );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 1 );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 4, frame.data() ) );   // the window is 5..8
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchHeldBeforeDecodeForTesting( 10000 ) );
    waitFor( [] { return mlvRawUint16PrefetchHeldCountForTesting() >= 2; }, 500 );

    std::vector<uint16_t> missed( words );
    int readerResult = -1;
    ReaderThread reader;
    mlvSetRawUint16ForegroundHoldBeforeDecodeForTesting( 1 );
    reader.thread = std::thread( [&]() { readerResult = getMlvRawFrameUint16( video, 7, missed.data() ); } );
    const bool readerParked = waitFor( [] { return mlvRawUint16ForegroundHeldCountForTesting() >= 1; }, 10000 );

    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    const bool workersIdle = mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) != 0;
    mlvSetRawUint16ForegroundHoldBeforeDecodeForTesting( 0 );
    reader.thread.join();
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );

    ASSERT_TRUE( readerParked );
    ASSERT_TRUE( workersIdle );
    ASSERT_EQ( 0, readerResult );
    for( uint64_t f = 0; f < frames && f < 16; ++f )
    {
        ASSERT_TRUE( mlvRawUint16DecodeCountForTesting( f ) <= 1u );
    }
    ASSERT_EQ( 1u, mlvRawUint16DecodeCountForTesting( 7 ) );
    ASSERT_EQ( 0u, prefetchStats( video ).duplicate_publishes );
    mlvCancelPreviewPrefetch( video );
}

// Drop-frame playback advances 1..3 frames at a time, so a reader keeps missing frames inside the window the workers
// are filling. Within one generation no frame is decoded twice, and every request is either a copy or one decode.
TEST(RawUint16PrefetchParallel, DropFramePlaybackDecodesEachFrameAtMostOnce)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 2 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );
    const uint64_t frames = getMlvFrames( video );
    const uint64_t span = frames < 16 ? frames : 16;
    ASSERT_TRUE( span > 12 );

    std::vector<uint16_t> frame( frameWords( fixture ) );
    mlvResetRawUint16PrefetchStats( video );
    mlvResetRawUint16DecodeCountsForTesting( 1 );
    uint64_t requests = 0;
    for( uint64_t f = 0, step = 0; f < span; f += static_cast<uint64_t>( kDropPattern[step++ % 4] ) )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, f, frame.data() ) );
        ++requests;
        std::this_thread::sleep_for( std::chrono::milliseconds( 3 ) );
    }
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );

    for( uint64_t f = 0; f < frames && f < MLV_RAW_UINT16_PREFETCH_SLOTS * 8u; ++f )
    {
        ASSERT_TRUE( mlvRawUint16DecodeCountForTesting( f ) <= 1u );
    }
    const mlvRawUint16PrefetchStats_t stats = prefetchStats( video );
    ASSERT_EQ( requests, stats.fg_hits + stats.fg_direct_decodes );
    ASSERT_EQ( 0u, stats.duplicate_publishes );
    mlvCancelPreviewPrefetch( video );
}

// Parallel decode only reorders work: every frame read sequentially or in the drop pattern is byte-identical to a
// serial decode with prefetch off.
TEST(RawUint16PrefetchParallel, PrefetchedFramesAreByteIdenticalToSerialDecode)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 0 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    const uint64_t frames = getMlvFrames( video );
    const uint64_t span = frames < 16 ? frames : 16;
    ASSERT_TRUE( span > 12 );

    const size_t words = frameWords( fixture );
    std::vector<std::vector<uint16_t>> reference( span, std::vector<uint16_t>( words ) );
    for( uint64_t f = 0; f < span; ++f )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, f, reference[f].data() ) );
        mlvCancelPreviewPrefetch( video );
    }

    mlvSetRawUint16PrefetchDecodersForTesting( 2 );
    std::vector<uint16_t> frame( words );
    for( uint64_t f = 0; f < span; ++f )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, f, frame.data() ) );
        ASSERT_TRUE( std::memcmp( frame.data(), reference[f].data(), words * sizeof( uint16_t ) ) == 0 );
    }
    for( uint64_t f = 0, step = 0; f < span; f += static_cast<uint64_t>( kDropPattern[step++ % 4] ) )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, f, frame.data() ) );
        ASSERT_TRUE( std::memcmp( frame.data(), reference[f].data(), words * sizeof( uint16_t ) ) == 0 );
    }
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );
    ASSERT_EQ( 0u, prefetchStats( video ).duplicate_publishes );
    mlvCancelPreviewPrefetch( video );
}

// MLVAPP_RAW_UINT16_PREFETCH_DECODERS=1 restores one prefetch decoder, and k = 0 keeps every decode on the caller.
TEST(RawUint16PrefetchParallel, KillSwitchRestoresOneDecoder)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 1 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );

    std::vector<uint16_t> frame( frameWords( fixture ) );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 1 );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 4, frame.data() ) );
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchHeldBeforeDecodeForTesting( 10000 ) );
    std::this_thread::sleep_for( std::chrono::milliseconds( 100 ) );   // room for a second decoder to park, if any
    const int held = mlvRawUint16PrefetchHeldCountForTesting();
    const int claims = mlvRawUint16PrefetchClaimsForTesting( video, 0, nullptr, 0 );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );
    mlvCancelPreviewPrefetch( video );
    ASSERT_EQ( 1, held );
    ASSERT_EQ( 1, claims );

    ASSERT_EQ( 1, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( "1" ) );
    ASSERT_EQ( 4, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( "4" ) );
    ASSERT_EQ( 4, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( "9" ) );
    ASSERT_EQ( 2, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( "x" ) );
    ASSERT_EQ( 2, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( "" ) );
    ASSERT_EQ( 2, mlvRawUint16PrefetchDecodersFromEnvValueForTesting( nullptr ) );

    MlvPipelineFixture serialFixture;
    mlvSetRawUint16PrefetchDecodersForTesting( 0 );
    mlvObject_t *serial = openLargeDualIso( &serialFixture );
    ASSERT_TRUE( serial != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( serial ) );   // the gate still admits the request
    mlvResetRawUint16PrefetchStats( serial );
    const uint64_t reads = 8;
    for( uint64_t f = 0; f < reads; ++f )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( serial, f, frame.data() ) );
        ASSERT_EQ( 0, getMlvLastRawUint16PrefetchHit() );
    }
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( serial, 10000 ) );
    const mlvRawUint16PrefetchStats_t stats = prefetchStats( serial );
    ASSERT_EQ( reads, stats.fg_direct_decodes );
    ASSERT_EQ( 0u, stats.worker_decodes );
    ASSERT_EQ( 0u, stats.decoders );
}

// The object-scoped counters carry the worker's own LJ92 time, which the decode thread's thread-locals never see.
TEST(RawUint16PrefetchParallel, StatsCountWorkerLj92Time)
{
    MlvPipelineFixture fixture;
    PrefetchKnobs knobs( 2 );
    mlvObject_t *video = openLargeDualIso( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );

    std::vector<uint16_t> frame( frameWords( fixture ) );
    mlvResetRawUint16PrefetchStats( video );
    for( uint64_t f = 0; f < 8; ++f )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, f, frame.data() ) );
        // Set on this (the caller's) thread whichever thread decoded the frame.
        ASSERT_TRUE( getMlvLastRawUint16Source() != MLV_RAW_UINT16_SOURCE_NONE );
        ASSERT_TRUE( getMlvLastRawUint16FrameLj92Milliseconds() > 0.0 );
    }
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );
    const mlvRawUint16PrefetchStats_t stats = prefetchStats( video );
    ASSERT_TRUE( stats.worker_decodes >= 1u );
    ASSERT_TRUE( stats.worker_lj92_ms_sum > 0.0 );
    ASSERT_EQ( 0u, stats.duplicate_publishes );
    ASSERT_EQ( 8u, stats.admitted_requests );
    mlvCancelPreviewPrefetch( video );
}
