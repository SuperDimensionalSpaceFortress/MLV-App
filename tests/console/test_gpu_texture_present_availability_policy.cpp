#include "../common/minitest.h"

#include "../../platform/qt/GpuTexturePresentAvailabilityPolicy.h"

// CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major 3 / sol major 2): on
// 2bc8cc0a, texture_present_upload_ms_basis read "measured" whenever the
// combined `available` flag (recon OR amaze) was true, even when only one
// component actually reported. This test fails on that behavior (it would
// classify a one-of-two-available frame as Measured) and passes once a
// partial reading can never be upgraded to Measured.
TEST(GpuTexturePresentAvailabilityPolicy, OnlyReconAvailableIsPartialNotMeasured)
{
    const GpuTexturePresentTimingBasis basis =
        GpuTexturePresentAvailabilityPolicy::classify(
            /*attempted=*/true, /*reconAvailable=*/true, /*amazeAvailable=*/false );
    ASSERT_TRUE(basis == GpuTexturePresentTimingBasis::Partial);
    ASSERT_FALSE(basis == GpuTexturePresentTimingBasis::Measured);
}

TEST(GpuTexturePresentAvailabilityPolicy, OnlyAmazeAvailableIsPartialNotMeasured)
{
    const GpuTexturePresentTimingBasis basis =
        GpuTexturePresentAvailabilityPolicy::classify(
            /*attempted=*/true, /*reconAvailable=*/false, /*amazeAvailable=*/true );
    ASSERT_TRUE(basis == GpuTexturePresentTimingBasis::Partial);
    ASSERT_FALSE(basis == GpuTexturePresentTimingBasis::Measured);
}

TEST(GpuTexturePresentAvailabilityPolicy, BothComponentsAvailableIsMeasured)
{
    const GpuTexturePresentTimingBasis basis =
        GpuTexturePresentAvailabilityPolicy::classify(
            /*attempted=*/true, /*reconAvailable=*/true, /*amazeAvailable=*/true );
    ASSERT_TRUE(basis == GpuTexturePresentTimingBasis::Measured);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra minor 6): on 2bc8cc0a, a frame
// where the texture-present path never ran at all (CPU-only debayer) read
// identically to an attempted-and-failed frame, because the absent
// telemetry key defaulted to false the same as a present-and-false key.
// This test fails on that conflation and passes once non-execution is its
// own state.
TEST(GpuTexturePresentAvailabilityPolicy, NotAttemptedIsDistinctFromUnavailable)
{
    const GpuTexturePresentTimingBasis notExecuted =
        GpuTexturePresentAvailabilityPolicy::classify(
            /*attempted=*/false, /*reconAvailable=*/false, /*amazeAvailable=*/false );
    const GpuTexturePresentTimingBasis unavailable =
        GpuTexturePresentAvailabilityPolicy::classify(
            /*attempted=*/true, /*reconAvailable=*/false, /*amazeAvailable=*/false );
    ASSERT_TRUE(notExecuted == GpuTexturePresentTimingBasis::NotExecuted);
    ASSERT_TRUE(unavailable == GpuTexturePresentTimingBasis::Unavailable);
    ASSERT_FALSE(notExecuted == unavailable);
}
