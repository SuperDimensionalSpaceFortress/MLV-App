// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row A: the paused prewarm keeps its warmed RGBA16 + R16 pair (still
// registered with the AMaZE CUDA-GL cache) as a staged slot, and the first recon present adopts it instead of
// re-allocating and re-registering. These pin when it is adopted (only when the size matches exactly, the old texture
// was never CUDA-registered and no recon source exists), when it is discarded, and the kill switch.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackRouteStagedSlotPolicy.h"

#include <string>

using playback_route_staged_slot::Decision;
using playback_route_staged_slot::decide;

TEST(PlaybackRouteStagedSlotPolicy, AStagedPairAtThePresentSizeIsAdoptedOverThePausedImageTexture)
{
    // staging on, a staged 1808x2268 pair, the present is 1808x2268, the old texture is the paused QImage one
    ASSERT_TRUE(Decision::Adopt == decide(true, true, 1808, 2268, 1808, 2268, false, false));
    ASSERT_EQ(std::string("adopted"), std::string(playback_route_staged_slot::decisionName(Decision::Adopt)));
}

TEST(PlaybackRouteStagedSlotPolicy, ASizeMismatchIsDiscardedNeverAdopted)
{
    // a scale change while paused: the staged pair is another size
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 904, 1134, false, false));
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 1808, 2267, false, false));
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 1807, 2268, false, false));
    ASSERT_EQ(std::string("discarded"), std::string(playback_route_staged_slot::decisionName(Decision::Discard)));
}

TEST(PlaybackRouteStagedSlotPolicy, ACurrentReconTextureIsNeverReplacedByTheStagedPair)
{
    // the old texture is a recon texture: it may hold a CUDA-GL registration, so today's destroyTexture() path runs
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 1808, 2268, true, false));
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 1808, 2268, true, true));
}

TEST(PlaybackRouteStagedSlotPolicy, AnExistingReconSourceTextureMeansDiscard)
{
    ASSERT_TRUE(Decision::Discard == decide(true, true, 1808, 2268, 1808, 2268, false, true));
}

TEST(PlaybackRouteStagedSlotPolicy, TheKillSwitchDiscardsAnyStagedPair)
{
    // MLVAPP_PLAYBACK_ROUTE_PREWARM_STAGE=0: nothing is adopted; a pair staged anyway is freed
    ASSERT_TRUE(Decision::Discard == decide(false, true, 1808, 2268, 1808, 2268, false, false));
    ASSERT_TRUE(Decision::None == decide(false, false, 0, 0, 1808, 2268, false, false));
}

TEST(PlaybackRouteStagedSlotPolicy, NoStagedPairMeansNothingToDo)
{
    ASSERT_TRUE(Decision::None == decide(true, false, 0, 0, 1808, 2268, false, false));
    ASSERT_TRUE(Decision::None == decide(true, false, 1808, 2268, 1808, 2268, false, false));
    ASSERT_TRUE(Decision::None == decide(true, false, 0, 0, 1808, 2268, true, true));
    ASSERT_EQ(std::string("none"), std::string(playback_route_staged_slot::decisionName(Decision::None)));
}

TEST(PlaybackRouteStagedSlotPolicy, AnEmptyPresentSizeIsNeverAdopted)
{
    ASSERT_TRUE(Decision::Discard == decide(true, true, 0, 0, 0, 0, false, false));
}
