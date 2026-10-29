// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F3: a slot buffer shorter than the dims imply is never handed out as the 8-bit source.
#include "../common/minitest.h"
#include "../../platform/qt/PlaybackPrepPresentationPolicy.h"

TEST(PlaybackPrepRgb8Source, ShortSlotBufferIsNotARgb8Source)
{
    uint8_t slot[12] = {};
    ASSERT_TRUE( playbackSelectRgb8Source( nullptr, 0, slot, sizeof slot, 24 ) == nullptr );
    ASSERT_TRUE( playbackSelectRgb8Source( slot, sizeof slot, slot, sizeof slot, 24 ) == nullptr );
}

TEST(PlaybackPrepRgb8Source, SizedSourceFirstThenFullSlotBuffer)
{
    uint8_t owned[24] = {};
    uint8_t slot[32] = {};
    ASSERT_TRUE( playbackSelectRgb8Source( owned, sizeof owned, slot, sizeof slot, 24 ) == owned );
    ASSERT_TRUE( playbackSelectRgb8Source( owned, 12, slot, sizeof slot, 24 ) == slot );
    ASSERT_TRUE( playbackSelectRgb8Source( nullptr, 0, slot, sizeof slot, 24 ) == slot );
    ASSERT_TRUE( playbackSelectRgb8Source( nullptr, 0, nullptr, 0, 24 ) == nullptr );
}
