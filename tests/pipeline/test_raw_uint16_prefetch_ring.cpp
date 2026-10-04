// LOOK-ASSIST-DUALISO-DETERMINISM-1: the raw-uint16 prefetch worker decodes into its claimed slot outside the mutex.
// A foreground store that reused that slot was labelled READY for its own frame and then overwritten by the worker's
// late decode, so every later read of that frame returned another frame's pixels (the headless Look Assist on
// large_dual_iso frame 10 flipped 6310 K -> 6300 K about once in 20 processes under load). Each test parks the worker
// on its claimed slot, drives the foreground around it, releases it, and reads the frames back.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include <QString>

#include <cstdint>
#include <vector>

namespace
{

// The worker prefetches two frames ahead of each request (not the playback-preview lookahead).
mlvObject_t *openLargeDualIsoWithPlainPrefetch( MlvPipelineFixture *fixture )
{
    mlvSetPlaybackAggressivePreviewMode( 0 );
    processingSetPlaybackPreviewMode( 0 );
    QString error_message;
    if( !fixture->openClipFile( repo_file_path( QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv") ), &error_message ) )
        return nullptr;
    fixture->video()->playback_scale_factor_active = 1;
    return fixture->video();
}

bool holdWorkerOnItsClaimedSlot()
{
    const int held = mlvWaitForRawUint16PrefetchHeldBeforeDecodeForTesting( 10000 );
    if( !held ) mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    return held != 0;
}

} // namespace

// The foreground stores one frame per slot while the worker is parked: before the fix the last store landed on it.
TEST(RawUint16PrefetchRing, NeverHandsOutTheSlotTheWorkerIsDecoding)
{
    MlvPipelineFixture fixture;
    mlvObject_t *video = openLargeDualIsoWithPlainPrefetch( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_TRUE( ( video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92 ) != 0 );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );
    ASSERT_EQ( 2u, mlvRawUint16PrefetchLookaheadForTesting( video ) );
    ASSERT_TRUE( getMlvFrames( video ) > 3 + MLV_RAW_UINT16_PREFETCH_SLOTS );

    const size_t words = static_cast<size_t>( fixture.width() ) * static_cast<size_t>( fixture.height() );
    const uint64_t firstStored = 3;
    std::vector<std::vector<uint16_t>> reference( MLV_RAW_UINT16_PREFETCH_SLOTS, std::vector<uint16_t>( words ) );
    for( uint64_t i = 0; i < MLV_RAW_UINT16_PREFETCH_SLOTS; ++i )
    {
        mlvCancelPreviewPrefetch( video );   // an empty ring: this read is a fresh decode, never a prefetched copy
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, firstStored + i, reference[i].data() ) );
    }
    mlvCancelPreviewPrefetch( video );

    // Frame 0 is a step back, so the ring resets; the worker then claims a slot for frame 1 and parks on it.
    std::vector<uint16_t> frame( words );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 1 );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 0, frame.data() ) );
    ASSERT_TRUE( holdWorkerOnItsClaimedSlot() );
    for( uint64_t i = 0; i < MLV_RAW_UINT16_PREFETCH_SLOTS; ++i )
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, firstStored + i, frame.data() ) );
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );

    // Newest first: a step back resets the ring, so reading an older frame first would discard the bad slot unread.
    for( uint64_t i = MLV_RAW_UINT16_PREFETCH_SLOTS; i-- > 0; )
    {
        ASSERT_EQ( 0, getMlvRawFrameUint16( video, firstStored + i, frame.data() ) );
        ASSERT_TRUE( frame == reference[i] );
    }
    mlvCancelPreviewPrefetch( video );
}

// The same hazard through the reset a step back makes: it used to clear the parked worker's slot, so the next foreground
// store could take it.
TEST(RawUint16PrefetchRing, ResetKeepsTheSlotTheWorkerIsDecoding)
{
    MlvPipelineFixture fixture;
    mlvObject_t *video = openLargeDualIsoWithPlainPrefetch( &fixture );
    ASSERT_TRUE( video != nullptr );
    ASSERT_EQ( 1, mlvRawUint16PrefetchAllowedForTesting( video ) );
    ASSERT_EQ( 2u, mlvRawUint16PrefetchLookaheadForTesting( video ) );

    const size_t words = static_cast<size_t>( fixture.width() ) * static_cast<size_t>( fixture.height() );
    std::vector<uint16_t> reference( words );
    std::vector<uint16_t> frame( words );
    mlvCancelPreviewPrefetch( video );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 3, reference.data() ) );
    mlvCancelPreviewPrefetch( video );

    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 1 );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 4, frame.data() ) );   // the worker claims a slot for frame 5 and parks
    ASSERT_TRUE( holdWorkerOnItsClaimedSlot() );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 2, frame.data() ) );   // a step back: the ring resets
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 2, frame.data() ) );
    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 3, frame.data() ) );   // before the fix: stored in the parked worker's slot
    mlvSetRawUint16PrefetchHoldBeforeDecodeForTesting( 0 );
    ASSERT_TRUE( mlvWaitForRawUint16PrefetchIdleForTesting( video, 10000 ) );

    ASSERT_EQ( 0, getMlvRawFrameUint16( video, 3, frame.data() ) );
    ASSERT_TRUE( frame == reference );
    mlvCancelPreviewPrefetch( video );
}
