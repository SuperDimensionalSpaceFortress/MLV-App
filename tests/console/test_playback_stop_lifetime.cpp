// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F1: a playback generation cancel must leave a Presenting slot to its borrower.
#include "../common/minitest.h"
#include "../../platform/qt/PlaybackSlotRelease.h"

TEST(PlaybackSlotRelease, CancelKeepsPresentingSlotPinned)
{
    ASSERT_FALSE( playbackCancelReleasesSlot( PlaybackSlotHold::Presenting, true, 7, 7 ) );
}

TEST(PlaybackSlotRelease, CancelReleasesReadySlotOfCancelledGeneration)
{
    ASSERT_TRUE( playbackCancelReleasesSlot( PlaybackSlotHold::Ready, true, 7, 7 ) );
}

TEST(PlaybackSlotRelease, CancelLeavesOtherGenerationsAndNonPlaybackSlots)
{
    ASSERT_FALSE( playbackCancelReleasesSlot( PlaybackSlotHold::Ready, true, 8, 7 ) );
    ASSERT_FALSE( playbackCancelReleasesSlot( PlaybackSlotHold::Ready, false, 7, 7 ) );
    ASSERT_FALSE( playbackCancelReleasesSlot( PlaybackSlotHold::Other, true, 7, 7 ) );
}
