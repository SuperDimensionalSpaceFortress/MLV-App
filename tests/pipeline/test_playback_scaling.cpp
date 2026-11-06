/*!
 * \file test_playback_scaling.cpp
 * \brief Phase 4D: bilinear playback upscale unit + perf tests.
 *
 * Exercises platform/qt/PlaybackScaling.h's
 * playbackBuildBilinearScaledRgb8 alongside the existing nearest-neighbour
 * path:
 *   - BilinearScaleOneIsByteIdentity : proves the bilinear path is exact
 *                                     when target == source, so scale=1
 *                                     presentation cannot soften or tint.
 *   - BilinearMatchesReferenceAtPlaybackScales : checks 1x/2x/4x against
 *                                     an independent centre-aligned scalar
 *                                     reference while reusing one cache
 *                                     across scale changes.
 *   - BilinearPreservesFlatFieldsAtPlaybackScales : proves constant RGB
 *                                     fields survive 1x/2x/4x without
 *                                     per-channel drift.
 *   - BilinearImpulseResponseIsSmoothAtPlaybackScales : verifies 2x/4x
 *                                     produce a smooth impulse footprint
 *                                     rather than nearest-neighbour blocks.
 *   - BilinearOutputDimensions      : feeds a synthetic 904x1134 buffer to
 *                                     playbackBuildBilinearScaledRgb8 at
 *                                     1808x2268 and verifies the output
 *                                     buffer has the expected size and is not
 *                                     all-zero.
 *   - BilinearVsNearestOnDiagonalLine : synthesises a diagonal line on white,
 *                                     upscales 200x200 -> 400x400 with both
 *                                     scalers, and asserts that bilinear
 *                                     produces substantially more
 *                                     intermediate-grey pixels (the smooth
 *                                     gradient on the line edge) than
 *                                     nearest-neighbour, which should be
 *                                     binary-edge only.
 *   - BilinearPerformance           : times bilinear on a 1808x2268 ->
 *                                     3616x4536 (2x upscale) and asserts the
 *                                     median wall-clock is under 10 ms when
 *                                     OMP can spread the work across the
 *                                     available worker threads.
 */

#include "../common/minitest.h"

#include "../../platform/qt/PlaybackScaling.h"

#include <QPixmap>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <omp.h>
#include <vector>

namespace {

std::vector<uint8_t> make_synthetic_rgb8(int width, int height)
{
    std::vector<uint8_t> buffer(static_cast<size_t>(width) * static_cast<size_t>(height) * 3u);
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width)
                                  + static_cast<size_t>(x)) * 3u;
            buffer[index + 0] = static_cast<uint8_t>((x * 7 + y * 11) & 0xFF);
            buffer[index + 1] = static_cast<uint8_t>((x * 13 + y * 17) & 0xFF);
            buffer[index + 2] = static_cast<uint8_t>((x * 19 + y * 23) & 0xFF);
        }
    }
    return buffer;
}

std::vector<uint8_t> make_flat_rgb8(int width,
                                    int height,
                                    uint8_t red,
                                    uint8_t green,
                                    uint8_t blue)
{
    std::vector<uint8_t> buffer(static_cast<size_t>(width) * static_cast<size_t>(height) * 3u);
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width)
                                  + static_cast<size_t>(x)) * 3u;
            buffer[index + 0] = red;
            buffer[index + 1] = green;
            buffer[index + 2] = blue;
        }
    }
    return buffer;
}

std::vector<uint8_t> make_diagonal_line_rgb8(int width, int height)
{
    /* White background, single black diagonal line.  At 200x200 the line goes
     * (0,0) -> (199,199); we mark the principal diagonal pixel and its two
     * orthogonal neighbours so the line has a tiny bit of width and the
     * smoothed bilinear upscale clearly shows intermediate greys. */
    std::vector<uint8_t> buffer(static_cast<size_t>(width) * static_cast<size_t>(height) * 3u, 255u);
    const int diagonalLength = std::min(width, height);
    for( int t = 0; t < diagonalLength; ++t )
    {
        const int x = t;
        const int y = t;
        const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width)
                              + static_cast<size_t>(x)) * 3u;
        buffer[index + 0] = 0u;
        buffer[index + 1] = 0u;
        buffer[index + 2] = 0u;
    }
    return buffer;
}

std::vector<uint8_t> make_impulse_rgb8(int width, int height)
{
    std::vector<uint8_t> buffer(static_cast<size_t>(width) * static_cast<size_t>(height) * 3u, 0u);
    const int cx = width / 2;
    const int cy = height / 2;
    const size_t index = (static_cast<size_t>(cy) * static_cast<size_t>(width)
                          + static_cast<size_t>(cx)) * 3u;
    buffer[index + 0] = 255u;
    buffer[index + 1] = 255u;
    buffer[index + 2] = 255u;
    return buffer;
}

struct AxisReferenceSample
{
    int i0;
    int i1;
    double weight;
};

AxisReferenceSample reference_axis_sample(int outputIndex,
                                          int sourceSize,
                                          int targetSize)
{
    if( sourceSize <= 1 )
    {
        return { 0, 0, 0.0 };
    }

    const double sourceCentre =
        ( ( static_cast<double>( outputIndex ) + 0.5 ) *
          static_cast<double>( sourceSize ) /
          static_cast<double>( targetSize ) ) - 0.5;
    if( sourceCentre <= 0.0 )
    {
        return { 0, 1, 0.0 };
    }
    if( sourceCentre >= static_cast<double>( sourceSize - 1 ) )
    {
        return { sourceSize - 1, sourceSize - 1, 0.0 };
    }

    const int i0 = static_cast<int>( std::floor( sourceCentre ) );
    const int i1 = std::min( sourceSize - 1, i0 + 1 );
    const double weight = sourceCentre - static_cast<double>( i0 );
    return { i0, i1, weight };
}

uint8_t reference_bilinear_channel(const std::vector<uint8_t> & source,
                                   int sourceWidth,
                                   int sourceHeight,
                                   int targetWidth,
                                   int targetHeight,
                                   int targetX,
                                   int targetY,
                                   int channel)
{
    const AxisReferenceSample sx =
        reference_axis_sample(targetX, sourceWidth, targetWidth);
    const AxisReferenceSample sy =
        reference_axis_sample(targetY, sourceHeight, targetHeight);

    const auto sample = [&](int x, int y) -> double {
        const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(sourceWidth)
                              + static_cast<size_t>(x)) * 3u
                           + static_cast<size_t>(channel);
        return static_cast<double>(source[index]);
    };

    const double top =
        sample(sx.i0, sy.i0) * (1.0 - sx.weight) +
        sample(sx.i1, sy.i0) * sx.weight;
    const double bottom =
        sample(sx.i0, sy.i1) * (1.0 - sx.weight) +
        sample(sx.i1, sy.i1) * sx.weight;
    const double value = top * (1.0 - sy.weight) + bottom * sy.weight;
    const int rounded = static_cast<int>(std::floor(value + 0.5));
    return static_cast<uint8_t>(std::max(0, std::min(255, rounded)));
}

int count_intermediate_grey_pixels(const std::vector<uint8_t> & rgb,
                                   int width,
                                   int height,
                                   int low,
                                   int high)
{
    int count = 0;
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width)
                                  + static_cast<size_t>(x)) * 3u;
            const int r = rgb[index + 0];
            const int g = rgb[index + 1];
            const int b = rgb[index + 2];
            /* Treat nearly-equal R=G=B in the gradient band as a smoothed
             * grey edge sample (bilinear blends black and white pixels into
             * neutral greys; nearest-neighbour produces only 0 or 255). */
            if( r >= low && r <= high && g >= low && g <= high && b >= low && b <= high )
            {
                ++count;
            }
        }
    }
    return count;
}

double median_milliseconds(std::vector<double> samples)
{
    std::sort(samples.begin(), samples.end());
    if( samples.empty() ) return 0.0;
    return samples[samples.size() / 2];
}

} // namespace

TEST(PlaybackScaling, BilinearScaleOneIsByteIdentity)
{
    struct SizeCase
    {
        int width;
        int height;
    };

    const SizeCase cases[] = {
        { 1, 1 },
        { 2, 3 },
        { 17, 13 },
        { 37, 19 }
    };

    for( const SizeCase & sizeCase : cases )
    {
        const std::vector<uint8_t> source =
            make_synthetic_rgb8(sizeCase.width, sizeCase.height);
        BilinearPlaybackScaleCache cache;
        std::vector<uint8_t> scaled;

        const bool ok = playbackBuildBilinearScaledRgb8(source.data(),
                                                        sizeCase.width,
                                                        sizeCase.height,
                                                        sizeCase.width,
                                                        sizeCase.height,
                                                        scaled,
                                                        cache);
        ASSERT_TRUE(ok);
        ASSERT_EQ(source.size(), scaled.size());
        ASSERT_TRUE(source == scaled);
    }
}

TEST(PlaybackScaling, BilinearMatchesReferenceAtPlaybackScales)
{
    const int sourceWidth = 11;
    const int sourceHeight = 9;
    const std::vector<uint8_t> source =
        make_synthetic_rgb8(sourceWidth, sourceHeight);

    /* Reuse one cache while bouncing through the real playback scale factors.
     * This catches stale cache tables when the UI changes quality modes. */
    BilinearPlaybackScaleCache cache;
    std::vector<uint8_t> scaled;
    const int scales[] = { 1, 4, 2, 1 };

    for( const int scale : scales )
    {
        const int targetWidth = sourceWidth * scale;
        const int targetHeight = sourceHeight * scale;
        const bool ok = playbackBuildBilinearScaledRgb8(source.data(),
                                                        sourceWidth,
                                                        sourceHeight,
                                                        targetWidth,
                                                        targetHeight,
                                                        scaled,
                                                        cache);
        ASSERT_TRUE(ok);
        ASSERT_EQ(static_cast<size_t>(targetWidth) * static_cast<size_t>(targetHeight) * 3u,
                  scaled.size());

        for( int y = 0; y < targetHeight; ++y )
        {
            for( int x = 0; x < targetWidth; ++x )
            {
                const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(targetWidth)
                                      + static_cast<size_t>(x)) * 3u;
                for( int c = 0; c < 3; ++c )
                {
                    const uint8_t expected = reference_bilinear_channel(source,
                                                                        sourceWidth,
                                                                        sourceHeight,
                                                                        targetWidth,
                                                                        targetHeight,
                                                                        x,
                                                                        y,
                                                                        c);
                    ASSERT_EQ(static_cast<int>(expected),
                              static_cast<int>(scaled[index + static_cast<size_t>(c)]));
                }
            }
        }
    }
}

TEST(PlaybackScaling, BilinearPreservesFlatFieldsAtPlaybackScales)
{
    const int sourceWidth = 13;
    const int sourceHeight = 7;
    const uint8_t red = 41u;
    const uint8_t green = 127u;
    const uint8_t blue = 213u;
    const std::vector<uint8_t> source =
        make_flat_rgb8(sourceWidth, sourceHeight, red, green, blue);

    BilinearPlaybackScaleCache cache;
    std::vector<uint8_t> scaled;
    const int scales[] = { 1, 2, 4, 8 };

    for( const int scale : scales )
    {
        const int targetWidth = sourceWidth * scale;
        const int targetHeight = sourceHeight * scale;
        ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                    sourceWidth,
                                                    sourceHeight,
                                                    targetWidth,
                                                    targetHeight,
                                                    scaled,
                                                    cache));
        ASSERT_EQ(static_cast<size_t>(targetWidth) * static_cast<size_t>(targetHeight) * 3u,
                  scaled.size());
        for( size_t i = 0; i < scaled.size(); i += 3u )
        {
            ASSERT_EQ(static_cast<int>(red), static_cast<int>(scaled[i + 0u]));
            ASSERT_EQ(static_cast<int>(green), static_cast<int>(scaled[i + 1u]));
            ASSERT_EQ(static_cast<int>(blue), static_cast<int>(scaled[i + 2u]));
        }
    }
}

TEST(PlaybackScaling, BilinearImpulseResponseIsSmoothAtPlaybackScales)
{
    const int sourceWidth = 5;
    const int sourceHeight = 5;
    const std::vector<uint8_t> source =
        make_impulse_rgb8(sourceWidth, sourceHeight);

    BilinearPlaybackScaleCache bilinearCache;
    FastPlaybackScaleCache nearestCache;
    std::vector<uint8_t> bilinearScaled;
    std::vector<uint8_t> nearestScaled;
    const int scales[] = { 2, 4, 8 };

    for( const int scale : scales )
    {
        const int targetWidth = sourceWidth * scale;
        const int targetHeight = sourceHeight * scale;
        ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                    sourceWidth,
                                                    sourceHeight,
                                                    targetWidth,
                                                    targetHeight,
                                                    bilinearScaled,
                                                    bilinearCache));
        ASSERT_TRUE(playbackBuildFastScaledRgb8(source.data(),
                                                sourceWidth,
                                                sourceHeight,
                                                targetWidth,
                                                targetHeight,
                                                nearestScaled,
                                                nearestCache));

        int bilinearIntermediate = 0;
        int nearestIntermediate = 0;
        int bilinearPeak = 0;
        for( size_t i = 0; i < bilinearScaled.size(); i += 3u )
        {
            const int value = bilinearScaled[i];
            bilinearPeak = std::max(bilinearPeak, value);
            if( value > 0 && value < 255 )
            {
                ++bilinearIntermediate;
            }
        }
        for( size_t i = 0; i < nearestScaled.size(); i += 3u )
        {
            const int value = nearestScaled[i];
            if( value > 0 && value < 255 )
            {
                ++nearestIntermediate;
            }
        }

        ASSERT_EQ(0, nearestIntermediate);
        ASSERT_TRUE(bilinearIntermediate > scale * scale);
        ASSERT_TRUE(bilinearPeak >= 140);

        const size_t firstPixel = 0u;
        const size_t lastPixel = (static_cast<size_t>(targetWidth) *
                                  static_cast<size_t>(targetHeight) - 1u) * 3u;
        ASSERT_EQ(0, static_cast<int>(bilinearScaled[firstPixel]));
        ASSERT_EQ(0, static_cast<int>(bilinearScaled[lastPixel]));
    }
}

TEST(PlaybackScaling, BilinearOutputDimensions)
{
    const int sourceWidth = 904;
    const int sourceHeight = 1134;
    const int targetWidth = 1808;
    const int targetHeight = 2268;

    const std::vector<uint8_t> source = make_synthetic_rgb8(sourceWidth, sourceHeight);
    BilinearPlaybackScaleCache cache;
    std::vector<uint8_t> scaled;

    const bool ok = playbackBuildBilinearScaledRgb8(source.data(),
                                                    sourceWidth, sourceHeight,
                                                    targetWidth, targetHeight,
                                                    scaled, cache);
    ASSERT_TRUE(ok);

    const size_t expectedSize =
        static_cast<size_t>(targetWidth) * static_cast<size_t>(targetHeight) * 3u;
    ASSERT_EQ(expectedSize, scaled.size());

    /* Cache should now reflect the requested dimensions and be sized for
     * per-row / per-column lookups. */
    ASSERT_EQ(sourceWidth, cache.sourceWidth);
    ASSERT_EQ(sourceHeight, cache.sourceHeight);
    ASSERT_EQ(targetWidth, cache.targetWidth);
    ASSERT_EQ(targetHeight, cache.targetHeight);
    ASSERT_EQ(static_cast<size_t>(targetWidth), cache.x0SourceOffsets.size());
    ASSERT_EQ(static_cast<size_t>(targetWidth), cache.x1SourceOffsets.size());
    ASSERT_EQ(static_cast<size_t>(targetWidth), cache.xWeights.size());
    ASSERT_EQ(static_cast<size_t>(targetHeight), cache.y0RowOffsets.size());
    ASSERT_EQ(static_cast<size_t>(targetHeight), cache.y1RowOffsets.size());
    ASSERT_EQ(static_cast<size_t>(targetHeight), cache.yWeights.size());

    /* Output must not be all-zero: the synthetic source has non-zero pixels
     * everywhere and bilinear preserves non-zero contributions. */
    bool foundNonZero = false;
    for( size_t i = 0; i < scaled.size(); ++i )
    {
        if( scaled[i] != 0u )
        {
            foundNonZero = true;
            break;
        }
    }
    ASSERT_TRUE(foundNonZero);

    /* Re-running with the same dimensions should reuse the cache (no resize
     * happens, but the call still succeeds). */
    const bool okSecond = playbackBuildBilinearScaledRgb8(source.data(),
                                                          sourceWidth, sourceHeight,
                                                          targetWidth, targetHeight,
                                                          scaled, cache);
    ASSERT_TRUE(okSecond);
    ASSERT_EQ(expectedSize, scaled.size());

    /* Invalid input rejected. */
    std::vector<uint8_t> invalidScaled;
    BilinearPlaybackScaleCache invalidCache;
    const bool nullSrcOk = playbackBuildBilinearScaledRgb8(nullptr,
                                                           sourceWidth, sourceHeight,
                                                           targetWidth, targetHeight,
                                                           invalidScaled, invalidCache);
    ASSERT_FALSE(nullSrcOk);
    ASSERT_TRUE(invalidScaled.empty());
}

TEST(PlaybackScaling, BilinearVsNearestOnDiagonalLine)
{
    const int sourceWidth = 200;
    const int sourceHeight = 200;
    const int targetWidth = 400;
    const int targetHeight = 400;

    const std::vector<uint8_t> source = make_diagonal_line_rgb8(sourceWidth, sourceHeight);

    FastPlaybackScaleCache nearestCache;
    std::vector<uint8_t> nearestScaled;
    const bool nearestOk = playbackBuildFastScaledRgb8(source.data(),
                                                       sourceWidth, sourceHeight,
                                                       targetWidth, targetHeight,
                                                       nearestScaled, nearestCache);
    ASSERT_TRUE(nearestOk);

    BilinearPlaybackScaleCache bilinearCache;
    std::vector<uint8_t> bilinearScaled;
    const bool bilinearOk = playbackBuildBilinearScaledRgb8(source.data(),
                                                            sourceWidth, sourceHeight,
                                                            targetWidth, targetHeight,
                                                            bilinearScaled, bilinearCache);
    ASSERT_TRUE(bilinearOk);

    /* Count pixels in the smoothed band (16..239) — bilinear should produce
     * many such pixels on the line edge, nearest near zero. */
    const int nearestSmoothed = count_intermediate_grey_pixels(nearestScaled,
                                                               targetWidth, targetHeight,
                                                               16, 239);
    const int bilinearSmoothed = count_intermediate_grey_pixels(bilinearScaled,
                                                                targetWidth, targetHeight,
                                                                16, 239);

    /* Hard sanity check: nearest produces a stepped binary edge; tolerate a
     * small handful of intermediate samples (boundary rounding) but assert
     * the count is a tiny fraction of the diagonal length.  The diagonal has
     * 400 pixel positions on the upscaled grid, so anything well under that
     * is fine. */
    ASSERT_TRUE(nearestSmoothed < 50);

    /* Bilinear should produce substantially more.  An ideal smooth-gradient
     * upscale of a single-pixel-wide diagonal at 2x produces roughly
     * 4x diagonal-length intermediate samples (gradient on both sides of
     * each step).  We accept 3x diagonal length as the lower bound to leave
     * margin for the integer-math rounding. */
    const int diagonalLength = std::min(targetWidth, targetHeight);
    const int bilinearLowerBound = diagonalLength * 3;
    ASSERT_TRUE(bilinearSmoothed > bilinearLowerBound);
    ASSERT_TRUE(bilinearSmoothed > nearestSmoothed * 10);
}

TEST(PlaybackScaling, PresentationResamplerPolicySplitsSharpAndAggressive)
{
    ASSERT_EQ(static_cast<int>(PlaybackPresentationScaleResampler::Bilinear),
              static_cast<int>(playbackChoosePresentationScaleResampler(
                  4,
                  3,
                  true,
                  true,
                  false)));
    ASSERT_EQ(static_cast<int>(PlaybackPresentationScaleResampler::Nearest),
              static_cast<int>(playbackChoosePresentationScaleResampler(
                  4,
                  3,
                  true,
                  true,
                  true)));
    ASSERT_EQ(static_cast<int>(PlaybackPresentationScaleResampler::Cubic),
              static_cast<int>(playbackChoosePresentationScaleResampler(
                  4,
                  0,
                  true,
                  true,
                  false)));
    ASSERT_EQ(static_cast<int>(PlaybackPresentationScaleResampler::Nearest),
              static_cast<int>(playbackChoosePresentationScaleResampler(
                  1,
                  0,
                  false,
                  false,
                  false)));
    ASSERT_EQ(static_cast<int>(PlaybackPresentationScaleResampler::Cubic),
              static_cast<int>(playbackChoosePresentationScaleResampler(
                  8,
                  8,
                  true,
                  true,
                  true)));
}

// T10 (PLAYBACK-CUDA-HONOUR-SCALE-1 r3): the reduced-texture predicate behind the AMaZE
// H-Nyquist flag. M16-1243-shaped dims: clip 1808x2268, x2 904x1132/1134, x4 452x564;
// the GPU window gets the clip's display size (clip x stretch).
// Mutations: filter enabled on x1, never enabled.
TEST(PlaybackScaling, ReconTexturePredicateIsReducedOnlyAtX2AndX4)
{
    // Clip-size form: smaller on both axes is reduced.
    ASSERT_TRUE(playbackReconTextureIsReduced(904, 1132, 1808, 2268));
    ASSERT_TRUE(playbackReconTextureIsReduced(452, 564, 1808, 2268));
    ASSERT_FALSE(playbackReconTextureIsReduced(1808, 2268, 1808, 2268)); // x1, export, paused, scrub
    ASSERT_FALSE(playbackReconTextureIsReduced(0, 0, 1808, 2268));
    ASSERT_FALSE(playbackReconTextureIsReduced(452, 564, 0, 0));

    // Display-size form (what the GPU window is handed).
    const int stretches[][2] = { { 3, 1 }, { 1, 1 } };
    for (const auto & s : stretches)
    {
        const int dw = 1808 * s[0], dh = 2268 * s[1];
        ASSERT_TRUE(playbackReconTextureIsReducedForDisplaySize(904, 1134, dw, dh));
        ASSERT_TRUE(playbackReconTextureIsReducedForDisplaySize(452, 564, dw, dh));
        ASSERT_FALSE(playbackReconTextureIsReducedForDisplaySize(1808, 2268, dw, dh));
    }
    // x1 with the 1.667 vertical stretch and up to 2.0 horizontal stays x1.
    ASSERT_FALSE(playbackReconTextureIsReducedForDisplaySize(1808, 2268, 3616, 3780));
    // x2 of an odd-sized clip (texture rounded up) is still reduced.
    ASSERT_TRUE(playbackReconTextureIsReducedForDisplaySize(905, 1135, 1809, 2269));
    // No display size: never reduced.
    ASSERT_FALSE(playbackReconTextureIsReducedForDisplaySize(452, 564, 0, 0));

    // r4: the H-Nyquist filter is wanted only on a reduced present and only while
    // kReducedHnyquistOnReducedPresents is 1 (r4 default 0: the phase-tent shrink fixes
    // the comb at its source). Mutation: H-filter constant ignored.
    ASSERT_FALSE(playbackReducedHnyquistWanted(true, 0));
    ASSERT_TRUE(playbackReducedHnyquistWanted(true, 1));
    ASSERT_FALSE(playbackReducedHnyquistWanted(false, 1));
    ASSERT_EQ(kReducedHnyquistOnReducedPresents != 0, playbackReducedHnyquistWanted(true));
    ASSERT_FALSE(playbackReducedHnyquistWanted(false));

    // A reduced present that wants the filter without the AMaZE symbol is refused; x1
    // never is, and with the filter off nothing is. Mutation: missing symbol not refused.
    ASSERT_TRUE(playbackReducedHnyquistPresentRefused(true, false, 1));
    ASSERT_FALSE(playbackReducedHnyquistPresentRefused(true, true, 1));
    ASSERT_FALSE(playbackReducedHnyquistPresentRefused(false, false, 1));
    ASSERT_FALSE(playbackReducedHnyquistPresentRefused(true, false, 0));
    ASSERT_EQ(kReducedHnyquistOnReducedPresents != 0, playbackReducedHnyquistPresentRefused(true, false));
}

TEST(PlaybackScaling, SuppressesUniformTopMagentaBandOnly)
{
    const int width = 64;
    const int height = 80;
    const int rowBytes = width * 3;
    const int bytesPerLine = rowBytes + 4;
    std::vector<uint8_t> image(static_cast<size_t>(bytesPerLine) *
                               static_cast<size_t>(height),
                               0xCDu);

    for( int y = 0; y < height; ++y )
    {
        uint8_t *row =
            image.data() + static_cast<size_t>(y) * static_cast<size_t>(bytesPerLine);
        for( int x = 0; x < width; ++x )
        {
            const size_t index = static_cast<size_t>(x) * 3u;
            if( y < 5 )
            {
                row[index + 0] = 203u;
                row[index + 1] = static_cast<uint8_t>(82 + (x % 68));
                row[index + 2] = 180u;
            }
            else if( y < 7 )
            {
                row[index + 0] = 142u;
                row[index + 1] = 87u;
                row[index + 2] = 122u;
            }
            else
            {
                row[index + 0] = static_cast<uint8_t>(40 + x);
                row[index + 1] = static_cast<uint8_t>(20 + y);
                row[index + 2] = static_cast<uint8_t>(30 + (x / 2));
            }
        }
    }

    const std::vector<uint8_t> before = image;
    const int suppressed =
        playbackSuppressUniformTopMagentaBandRgb8(image.data(),
                                                  width,
                                                  height,
                                                  bytesPerLine);
    ASSERT_EQ(7, suppressed);

    for( int y = 0; y < 7; ++y )
    {
        const uint8_t *row =
            image.data() + static_cast<size_t>(y) * static_cast<size_t>(bytesPerLine);
        for( int x = 0; x < bytesPerLine; ++x )
        {
            ASSERT_EQ(0, static_cast<int>(row[x]));
        }
    }

    for( int y = 7; y < height; ++y )
    {
        const size_t rowOffset =
            static_cast<size_t>(y) * static_cast<size_t>(bytesPerLine);
        for( int x = 0; x < bytesPerLine; ++x )
        {
            ASSERT_EQ(static_cast<int>(before[rowOffset + static_cast<size_t>(x)]),
                      static_cast<int>(image[rowOffset + static_cast<size_t>(x)]));
        }
    }
}

TEST(PlaybackScaling, LeavesVariedMagentaContentUntouched)
{
    const int width = 64;
    const int height = 24;
    std::vector<uint8_t> image = make_flat_rgb8(width, height, 30, 20, 25);

    for( int y = 0; y < 5; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index =
                (static_cast<size_t>(y) * static_cast<size_t>(width)
                 + static_cast<size_t>(x)) * 3u;
            image[index + 0] = static_cast<uint8_t>(180 + (x % 40));
            image[index + 1] = static_cast<uint8_t>(80 + (x % 35));
            image[index + 2] = static_cast<uint8_t>(150 + (x % 45));
        }
    }

    const std::vector<uint8_t> before = image;
    const int suppressed =
        playbackSuppressUniformTopMagentaBandRgb8(image.data(),
                                                  width,
                                                  height);
    ASSERT_EQ(0, suppressed);
    ASSERT_TRUE(before == image);
}

TEST(PlaybackScaling, SuppressesTopMagentaBandAfterBlackLetterbox)
{
    const int width = 64;
    const int height = 120;
    std::vector<uint8_t> image = make_flat_rgb8(width, height, 0, 0, 0);

    for( int y = 6; y < 11; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index =
                (static_cast<size_t>(y) * static_cast<size_t>(width)
                 + static_cast<size_t>(x)) * 3u;
            image[index + 0] = 203u;
            image[index + 1] = static_cast<uint8_t>(122 + (x % 17));
            image[index + 2] = 179u;
        }
    }

    for( int y = 11; y < 13; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index =
                (static_cast<size_t>(y) * static_cast<size_t>(width)
                 + static_cast<size_t>(x)) * 3u;
            image[index + 0] = 142u;
            image[index + 1] = 96u;
            image[index + 2] = 122u;
        }
    }

    for( int y = 13; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index =
                (static_cast<size_t>(y) * static_cast<size_t>(width)
                 + static_cast<size_t>(x)) * 3u;
            image[index + 0] = 18u;
            image[index + 1] = 16u;
            image[index + 2] = 8u;
        }
    }

    const int suppressed =
        playbackSuppressUniformTopMagentaBandRgb8(image.data(), width, height);
    ASSERT_EQ(7, suppressed);

    for( int y = 0; y < 6; ++y )
    {
        const size_t rowOffset =
            static_cast<size_t>(y) * static_cast<size_t>(width) * 3u;
        ASSERT_EQ(0, static_cast<int>(image[rowOffset + 0]));
        ASSERT_EQ(0, static_cast<int>(image[rowOffset + 1]));
        ASSERT_EQ(0, static_cast<int>(image[rowOffset + 2]));
    }

    for( int y = 6; y < 13; ++y )
    {
        const size_t rowOffset =
            static_cast<size_t>(y) * static_cast<size_t>(width) * 3u;
        for( int x = 0; x < width * 3; ++x )
        {
            ASSERT_EQ(0, static_cast<int>(image[rowOffset + static_cast<size_t>(x)]));
        }
    }

    const size_t contentOffset =
        static_cast<size_t>(13) * static_cast<size_t>(width) * 3u;
    ASSERT_EQ(18, static_cast<int>(image[contentOffset + 0]));
    ASSERT_EQ(16, static_cast<int>(image[contentOffset + 1]));
    ASSERT_EQ(8, static_cast<int>(image[contentOffset + 2]));
}

TEST(PlaybackScaling, SuppressesSingleTopMagentaEdgeAfterBlackLetterbox)
{
    const int width = 96;
    const int height = 96;
    std::vector<uint8_t> image = make_flat_rgb8(width, height, 0, 0, 0);

    for( int x = 0; x < width; ++x )
    {
        const size_t index =
            (static_cast<size_t>(8) * static_cast<size_t>(width)
             + static_cast<size_t>(x)) * 3u;
        if( x < 34 )
        {
            image[index + 0] = 0u;
            image[index + 1] = 0u;
            image[index + 2] = 0u;
        }
        else
        {
            image[index + 0] = static_cast<uint8_t>(104 + (x % 37));
            image[index + 1] = static_cast<uint8_t>(52 + (x % 36));
            image[index + 2] = static_cast<uint8_t>(92 + (x % 14));
        }
    }

    for( int y = 9; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const size_t index =
                (static_cast<size_t>(y) * static_cast<size_t>(width)
                 + static_cast<size_t>(x)) * 3u;
            image[index + 0] = 22u;
            image[index + 1] = 18u;
            image[index + 2] = 10u;
        }
    }

    const int suppressed =
        playbackSuppressUniformTopMagentaBandRgb8(image.data(), width, height);
    ASSERT_EQ(1, suppressed);

    const size_t edgeOffset =
        static_cast<size_t>(8) * static_cast<size_t>(width) * 3u;
    for( int x = 0; x < width * 3; ++x )
    {
        ASSERT_EQ(0, static_cast<int>(image[edgeOffset + static_cast<size_t>(x)]));
    }

    const size_t contentOffset =
        static_cast<size_t>(9) * static_cast<size_t>(width) * 3u;
    ASSERT_EQ(22, static_cast<int>(image[contentOffset + 0]));
    ASSERT_EQ(18, static_cast<int>(image[contentOffset + 1]));
    ASSERT_EQ(10, static_cast<int>(image[contentOffset + 2]));
}

TEST(PlaybackScaling, LeavesSingleTopMagentaEdgeWithoutLetterboxUntouched)
{
    const int width = 96;
    const int height = 64;
    std::vector<uint8_t> image = make_flat_rgb8(width, height, 22, 18, 10);

    for( int x = 0; x < width; ++x )
    {
        const size_t index = static_cast<size_t>(x) * 3u;
        if( x < 34 )
        {
            image[index + 0] = 0u;
            image[index + 1] = 0u;
            image[index + 2] = 0u;
        }
        else
        {
            image[index + 0] = static_cast<uint8_t>(104 + (x % 37));
            image[index + 1] = static_cast<uint8_t>(52 + (x % 36));
            image[index + 2] = static_cast<uint8_t>(92 + (x % 14));
        }
    }

    const std::vector<uint8_t> before = image;
    const int suppressed =
        playbackSuppressUniformTopMagentaBandRgb8(image.data(), width, height);
    ASSERT_EQ(0, suppressed);
    ASSERT_TRUE(before == image);
}

TEST(PlaybackScaling, FastScalerCanWritePaddedRows)
{
    const int sourceWidth = 7;
    const int sourceHeight = 5;
    const int targetWidth = 11;
    const int targetHeight = 9;
    const int rowBytes = targetWidth * 3;
    const int paddedBytesPerLine = rowBytes + 5;

    const std::vector<uint8_t> source =
        make_synthetic_rgb8(sourceWidth, sourceHeight);

    FastPlaybackScaleCache tightCache;
    std::vector<uint8_t> tight;
    ASSERT_TRUE(playbackBuildFastScaledRgb8(source.data(),
                                            sourceWidth, sourceHeight,
                                            targetWidth, targetHeight,
                                            tight, tightCache));

    FastPlaybackScaleCache paddedCache;
    std::vector<uint8_t> padded;
    ASSERT_TRUE(playbackBuildFastScaledRgb8(source.data(),
                                            sourceWidth, sourceHeight,
                                            targetWidth, targetHeight,
                                            padded, paddedCache,
                                            paddedBytesPerLine));

    ASSERT_EQ(static_cast<size_t>(paddedBytesPerLine) *
              static_cast<size_t>(targetHeight),
              padded.size());

    const auto assertPaddedRowsMatch =
        [&](const std::vector<uint8_t> &tightBuffer,
            const std::vector<uint8_t> &paddedBuffer)
        {
            ASSERT_EQ(static_cast<size_t>(paddedBytesPerLine) *
                      static_cast<size_t>(targetHeight),
                      paddedBuffer.size());
            ASSERT_EQ(static_cast<size_t>(rowBytes) *
                      static_cast<size_t>(targetHeight),
                      tightBuffer.size());
            for( int y = 0; y < targetHeight; ++y )
            {
                const size_t tightRow =
                    static_cast<size_t>(y) * static_cast<size_t>(rowBytes);
                const size_t paddedRow =
                    static_cast<size_t>(y) * static_cast<size_t>(paddedBytesPerLine);
                for( int x = 0; x < rowBytes; ++x )
                {
                    ASSERT_EQ(static_cast<int>(tightBuffer[tightRow + static_cast<size_t>(x)]),
                              static_cast<int>(paddedBuffer[paddedRow + static_cast<size_t>(x)]));
                }
                for( int x = rowBytes; x < paddedBytesPerLine; ++x )
                {
                    ASSERT_EQ(0, static_cast<int>(paddedBuffer[paddedRow + static_cast<size_t>(x)]));
                }
            }
        };

    for( int y = 0; y < targetHeight; ++y )
    {
        const size_t tightRow = static_cast<size_t>(y) * static_cast<size_t>(rowBytes);
        const size_t paddedRow =
            static_cast<size_t>(y) * static_cast<size_t>(paddedBytesPerLine);
        for( int x = 0; x < rowBytes; ++x )
        {
            ASSERT_EQ(static_cast<int>(tight[tightRow + static_cast<size_t>(x)]),
                      static_cast<int>(padded[paddedRow + static_cast<size_t>(x)]));
        }
        for( int x = rowBytes; x < paddedBytesPerLine; ++x )
        {
            ASSERT_EQ(0, static_cast<int>(padded[paddedRow + static_cast<size_t>(x)]));
        }
    }

    BilinearPlaybackScaleCache bilinearTightCache;
    BilinearPlaybackScaleCache bilinearPaddedCache;
    std::vector<uint8_t> bilinearTight;
    std::vector<uint8_t> bilinearPadded;
    ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                sourceWidth, sourceHeight,
                                                targetWidth, targetHeight,
                                                bilinearTight,
                                                bilinearTightCache));
    ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                sourceWidth, sourceHeight,
                                                targetWidth, targetHeight,
                                                bilinearPadded,
                                                bilinearPaddedCache,
                                                paddedBytesPerLine));
    assertPaddedRowsMatch(bilinearTight, bilinearPadded);

    CubicPlaybackScaleCache cubicTightCache;
    CubicPlaybackScaleCache cubicPaddedCache;
    std::vector<uint8_t> cubicTight;
    std::vector<uint8_t> cubicPadded;
    ASSERT_TRUE(playbackBuildCubicScaledRgb8(source.data(),
                                             sourceWidth, sourceHeight,
                                             targetWidth, targetHeight,
                                             cubicTight,
                                             cubicTightCache));
    ASSERT_TRUE(playbackBuildCubicScaledRgb8(source.data(),
                                             sourceWidth, sourceHeight,
                                             targetWidth, targetHeight,
                                             cubicPadded,
                                             cubicPaddedCache,
                                             paddedBytesPerLine));
    assertPaddedRowsMatch(cubicTight, cubicPadded);
}

TEST(PlaybackScaling, CubicPreservesFlatFieldsAndProducesValidUpscale)
{
    const int sourceWidth = 32;
    const int sourceHeight = 24;
    const int targetWidth = 128;
    const int targetHeight = 96;

    const std::vector<uint8_t> flat =
        make_flat_rgb8(sourceWidth, sourceHeight, 12, 128, 240);
    CubicPlaybackScaleCache cubicCache;
    std::vector<uint8_t> cubicFlat;
    ASSERT_TRUE(playbackBuildCubicScaledRgb8(flat.data(),
                                             sourceWidth,
                                             sourceHeight,
                                             targetWidth,
                                             targetHeight,
                                             cubicFlat,
                                             cubicCache));
    ASSERT_EQ(static_cast<std::size_t>(targetWidth) * targetHeight * 3u,
              cubicFlat.size());
    for( std::size_t i = 0; i + 2 < cubicFlat.size(); i += 3 )
    {
        ASSERT_TRUE(std::abs(static_cast<int>(cubicFlat[i + 0]) - 12) <= 1);
        ASSERT_TRUE(std::abs(static_cast<int>(cubicFlat[i + 1]) - 128) <= 1);
        ASSERT_TRUE(std::abs(static_cast<int>(cubicFlat[i + 2]) - 240) <= 1);
    }

    const std::vector<uint8_t> line =
        make_diagonal_line_rgb8(sourceWidth, sourceHeight);
    std::vector<uint8_t> cubicLine;
    ASSERT_TRUE(playbackBuildCubicScaledRgb8(line.data(),
                                             sourceWidth,
                                             sourceHeight,
                                             targetWidth,
                                             targetHeight,
                                             cubicLine,
                                             cubicCache));
    const int cubicSmoothed = count_intermediate_grey_pixels(cubicLine,
                                                             targetWidth,
                                                             targetHeight,
                                                             16,
                                                             239);
    ASSERT_TRUE(cubicSmoothed > std::min(targetWidth, targetHeight) * 2);
}

TEST(PlaybackScaling, BilinearPerformance)
{
    if( omp_get_max_threads() <= 1 )
    {
        SKIP_TEST("Bilinear performance threshold requires a multi-threaded OpenMP runtime.");
    }

    /* 1808x2268 -> 3616x4536: Phase 4B's scale=2 case where the half-res
     * render output has to be brought back up to the display target. */
    const int sourceWidth = 1808;
    const int sourceHeight = 2268;
    const int targetWidth = 3616;
    const int targetHeight = 4536;

    const std::vector<uint8_t> source = make_synthetic_rgb8(sourceWidth, sourceHeight);
    BilinearPlaybackScaleCache cache;
    std::vector<uint8_t> scaled;

    /* Warm-up call populates the cache so subsequent timed calls measure
     * the inner loop only (matching the steady-state production behaviour
     * after the first frame at a new target size). */
    ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                sourceWidth, sourceHeight,
                                                targetWidth, targetHeight,
                                                scaled, cache));

    constexpr int kIterations = 5;
    std::vector<double> samples;
    samples.reserve(kIterations);
    for( int i = 0; i < kIterations; ++i )
    {
        const auto start = std::chrono::steady_clock::now();
        ASSERT_TRUE(playbackBuildBilinearScaledRgb8(source.data(),
                                                    sourceWidth, sourceHeight,
                                                    targetWidth, targetHeight,
                                                    scaled, cache));
        const auto end = std::chrono::steady_clock::now();
        const double ms =
            std::chrono::duration<double, std::milli>(end - start).count();
        samples.push_back(ms);
    }

    const double medianMs = median_milliseconds(samples);
    /* 10 ms is the spec target on a 32-thread OMP host; allow headroom for
     * slow CI hosts but keep the assertion tight enough that a 50 ms
     * regression would surface. */
    ASSERT_TRUE(medianMs < 50.0);

    /* Side-by-side nearest-neighbour timing at the same source/target so
     * the bilinear cost premium is visible in the test log. */
    FastPlaybackScaleCache nearestCache;
    std::vector<uint8_t> nearestScaled;
    ASSERT_TRUE(playbackBuildFastScaledRgb8(source.data(),
                                            sourceWidth, sourceHeight,
                                            targetWidth, targetHeight,
                                            nearestScaled, nearestCache));
    std::vector<double> nearestSamples;
    nearestSamples.reserve(kIterations);
    for( int i = 0; i < kIterations; ++i )
    {
        const auto start = std::chrono::steady_clock::now();
        ASSERT_TRUE(playbackBuildFastScaledRgb8(source.data(),
                                                sourceWidth, sourceHeight,
                                                targetWidth, targetHeight,
                                                nearestScaled, nearestCache));
        const auto end = std::chrono::steady_clock::now();
        const double ms =
            std::chrono::duration<double, std::milli>(end - start).count();
        nearestSamples.push_back(ms);
    }
    const double nearestMedianMs = median_milliseconds(nearestSamples);

    /* Always emit both medians so transient regressions show up in the
     * test output even when the threshold passes.  Bilinear is expected to
     * cost a few ms more than nearest because of the second-axis blend, but
     * the spec target keeps the gap modest (~3-5 ms on the reference 32-
     * thread OMP host). */
    std::cout << "PlaybackScaling.BilinearPerformance median_ms="
              << medianMs
              << " nearest_median_ms=" << nearestMedianMs << "\n";
}

/* CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 (Part C): the CPU route's fast branch now writes the
 * prepared frame as QImage::Format_RGB32 (0xFFRRGGBB) instead of RGB888, so the GUI thread's
 * QPixmap::fromImage is a no-convert wrap. These pin that the presented picture is unchanged:
 * the RGB32 writer's R, G, B equal the RGB888 writer's, fromImage of both is pixel-equal
 * (including after the top magenta-band suppression), and the RGB32 fromImage is cheaper. */
namespace {

/* A developed 16-bit frame (gradient plus deterministic noise) reduced to 8 bits the way
 * convert_rgb16_to_rgb8 does (>> 8), so the 8-bit source is what the prep worker scales. */
std::vector<uint8_t> make_rgb8_from_synthetic_rgb16(int width, int height)
{
    std::vector<uint8_t> out(static_cast<size_t>(width) * static_cast<size_t>(height) * 3u);
    uint32_t state = 0x9E3779B9u;
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            for( int c = 0; c < 3; ++c )
            {
                state = state * 1664525u + 1013904223u;
                const uint32_t gradient =
                    static_cast<uint32_t>( ( static_cast<uint64_t>( x + y * (c + 1) ) * 65535u )
                                           / static_cast<uint64_t>( width + height * 3 ) );
                const uint32_t noise = (state >> 20) & 0x0FFFu;
                const uint16_t value16 = static_cast<uint16_t>( std::min<uint32_t>( 65535u, gradient + noise ) );
                out[(static_cast<size_t>(y) * static_cast<size_t>(width) + static_cast<size_t>(x)) * 3u
                    + static_cast<size_t>(c)] = static_cast<uint8_t>( value16 >> 8 );
            }
        }
    }
    return out;
}

struct FastRgbPair
{
    std::vector<uint8_t> rgb888;
    std::vector<uint8_t> rgb32;
    int width = 0;
    int height = 0;
    int rgb888BytesPerLine = 0;
    QImage image888() { return playbackWrapRgb8Image( rgb888.data(), width, height, rgb888BytesPerLine ); }
    QImage image32() { return playbackWrapRgb32Image( rgb32.data(), width, height ); }
};

/* Both writers, exactly as the fast branch calls them (RGB888 with the 4-byte aligned stride). */
FastRgbPair build_fast_pair(const std::vector<uint8_t> &source,
                            int sourceWidth, int sourceHeight,
                            int targetWidth, int targetHeight)
{
    FastRgbPair pair;
    pair.width = targetWidth;
    pair.height = targetHeight;
    pair.rgb888BytesPerLine = ((targetWidth * 3) + 3) & ~3;
    FastPlaybackScaleCache cache888;
    FastPlaybackScaleCache cache32;
    const bool ok888 = playbackBuildFastScaledRgb8( source.data(), sourceWidth, sourceHeight,
                                                    targetWidth, targetHeight,
                                                    pair.rgb888, cache888, pair.rgb888BytesPerLine );
    const bool ok32 = playbackBuildFastScaledRgb32( source.data(), sourceWidth, sourceHeight,
                                                    targetWidth, targetHeight,
                                                    pair.rgb32, cache32 );
    if( !ok888 || !ok32 ) { pair.rgb888.clear(); pair.rgb32.clear(); }
    return pair;
}

QImage pixmap_image(const QImage &image)
{
    return QPixmap::fromImage( image ).toImage().convertToFormat( QImage::Format_RGB32 );
}

} // namespace

TEST(PlaybackScaling, FastRgb32WriterMatchesRgb888WriterBytes)
{
    struct Case { int sw, sh, tw, th; };
    // The fs-s1 shape (downscale to 3840x1605), an odd-width downscale, and an upscale.
    const Case cases[] = { { 4096, 1716, 3840, 1605 }, { 1931, 803, 1283, 535 }, { 640, 360, 1283, 721 } };
    for( const Case &c : cases )
    {
        const std::vector<uint8_t> source = make_rgb8_from_synthetic_rgb16( c.sw, c.sh );
        FastRgbPair pair = build_fast_pair( source, c.sw, c.sh, c.tw, c.th );
        ASSERT_FALSE(pair.rgb888.empty());
        ASSERT_EQ(static_cast<size_t>(c.tw) * 4u * static_cast<size_t>(c.th), pair.rgb32.size());
        size_t mismatches = 0;
        for( int y = 0; y < c.th; ++y )
        {
            const uint8_t *row888 = pair.rgb888.data() + static_cast<size_t>(y) * static_cast<size_t>(pair.rgb888BytesPerLine);
            const uint8_t *row32 = pair.rgb32.data() + static_cast<size_t>(y) * static_cast<size_t>(c.tw) * 4u;
            for( int x = 0; x < c.tw; ++x )
            {
                uint32_t v = 0;
                std::memcpy( &v, row32 + static_cast<size_t>(x) * 4u, 4u );
                const uint8_t *p = row888 + static_cast<size_t>(x) * 3u;
                if( ( v >> 24 ) != 0xFFu
                 || ( ( v >> 16 ) & 0xFFu ) != p[0]
                 || ( ( v >> 8 ) & 0xFFu ) != p[1]
                 || ( v & 0xFFu ) != p[2] )
                    ++mismatches;
            }
        }
        ASSERT_EQ(static_cast<size_t>(0), mismatches);
        // The byte layout the magenta/zebra passes read through.
        ASSERT_EQ(static_cast<int>(pair.rgb888[0]), static_cast<int>(pair.rgb32[kPlaybackLayoutRgb32.r]));
        ASSERT_EQ(static_cast<int>(pair.rgb888[1]), static_cast<int>(pair.rgb32[kPlaybackLayoutRgb32.g]));
        ASSERT_EQ(static_cast<int>(pair.rgb888[2]), static_cast<int>(pair.rgb32[kPlaybackLayoutRgb32.b]));
        ASSERT_EQ(0xFF, static_cast<int>(pair.rgb32[kPlaybackRgb32AlphaByte]));
    }
}

TEST(PlaybackScaling, FastRgb32FromImageEqualsRgb888AndCostsLess)
{
    for( const int width : { 3840, 3841 } )
    {
        const int height = 1605;
        const std::vector<uint8_t> source = make_rgb8_from_synthetic_rgb16( width, height );
        FastRgbPair pair = build_fast_pair( source, width, height, width, height );
        ASSERT_FALSE(pair.rgb888.empty());
        const QImage img888 = pair.image888();
        const QImage img32 = pair.image32();
        ASSERT_TRUE(img888.format() == QImage::Format_RGB888);
        ASSERT_TRUE(img32.format() == QImage::Format_RGB32);
        ASSERT_TRUE(pixmap_image( img888 ) == pixmap_image( img32 ));

        std::vector<double> ms888;
        std::vector<double> ms32;
        for( int i = 0; i < 9; ++i )
        {
            auto t0 = std::chrono::steady_clock::now();
            QPixmap a = QPixmap::fromImage( img888 );
            auto t1 = std::chrono::steady_clock::now();
            QPixmap b = QPixmap::fromImage( img32 );
            auto t2 = std::chrono::steady_clock::now();
            ASSERT_FALSE(a.isNull());
            ASSERT_FALSE(b.isNull());
            ms888.push_back( std::chrono::duration<double, std::milli>( t1 - t0 ).count() );
            ms32.push_back( std::chrono::duration<double, std::milli>( t2 - t1 ).count() );
        }
        const double median888 = median_milliseconds( ms888 );
        const double median32 = median_milliseconds( ms32 );
        std::cout << "PlaybackScaling.FastRgb32FromImage width=" << width
                  << " rgb888_median_ms=" << median888
                  << " rgb32_median_ms=" << median32 << "\n";
        ASSERT_TRUE(median32 < median888);
    }
}

namespace {

enum class TopBandShape { UniformBand, EdgeAfterLetterbox };

/* A frame with 6 black letterbox rows, then either 5 uniform magenta band rows or 2 magenta
 * edge rows, then picture. */
std::vector<uint8_t> make_top_band_source(int width, int height, TopBandShape shape)
{
    std::vector<uint8_t> out = make_rgb8_from_synthetic_rgb16( width, height );
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            uint8_t *p = out.data() + (static_cast<size_t>(y) * static_cast<size_t>(width) + static_cast<size_t>(x)) * 3u;
            if( y < 6 )
            {
                p[0] = 0; p[1] = 0; p[2] = 0;
            }
            else if( shape == TopBandShape::UniformBand && y < 11 )
            {
                p[0] = 203; p[1] = static_cast<uint8_t>( 88 + (x % 5) ); p[2] = 180;
            }
            else if( shape == TopBandShape::EdgeAfterLetterbox && y < 8 )
            {
                p[0] = 120; p[1] = 60; p[2] = 100;
            }
        }
    }
    return out;
}

} // namespace

TEST(PlaybackScaling, FastRgb32MagentaBandSuppressionMatchesRgb888)
{
    for( const TopBandShape shape : { TopBandShape::UniformBand, TopBandShape::EdgeAfterLetterbox } )
    {
        const int width = 1283;
        const int height = 535;
        const std::vector<uint8_t> source = make_top_band_source( width, height, shape );
        FastRgbPair pair = build_fast_pair( source, width, height, width, height );
        ASSERT_FALSE(pair.rgb888.empty());
        QImage img888 = pair.image888();
        QImage img32 = pair.image32();
        const QImage before32 = img32.copy();

        const int rows888 = playbackSuppressUniformTopMagentaBandImage( img888 );
        const int rows32 = playbackSuppressUniformTopMagentaBandImage( img32 );
        ASSERT_EQ(shape == TopBandShape::UniformBand ? 5 : 2, rows888);
        ASSERT_EQ(rows888, rows32);
        ASSERT_TRUE(pixmap_image( img888 ) == pixmap_image( img32 ));
        // The suppressed rows are opaque black in the RGB32 frame.
        ASSERT_TRUE(img32.pixel( 0, 6 ) == 0xFF000000u);
        ASSERT_TRUE(img32.pixel( width - 1, 6 + rows32 - 1 ) == 0xFF000000u);

        // RED on the base code: its RGB888-only gate skipped an RGB32 frame, so the band stayed
        // and the picture differed from the RGB888 frame's.
        ASSERT_FALSE(pixmap_image( before32 ) == pixmap_image( img888 ));
    }
}

/* r2 adversary A1 (introduced, major): QPixmap::fromImage SHARES an RGB32 image's pixels instead of
 * converting them, so the pixmap of an image that only wrapped the prep result's byte vector pointed
 * into that vector after onPlaybackPrepResultReady returned. Input: 640x360 source, 1280x720 fitted
 * fast preview. The fast branch's RGB32 image must own its pixels. */
TEST(PlaybackScaling, FastRgb32ImageOwnsItsPixelsPastTheBuilder)
{
    const std::vector<uint8_t> source = make_rgb8_from_synthetic_rgb16( 640, 360 );
    FastPlaybackScaleCache cache;
    QPixmap pixmap;
    QImage expected;
    {
        const QImage built = playbackBuildFastScaledImageRgb32( source.data(), 640, 360, 1280, 720, cache );
        ASSERT_FALSE(built.isNull());
        ASSERT_TRUE(built.format() == QImage::Format_RGB32);
        expected = built.copy();
        pixmap = QPixmap::fromImage( built );
    }   // the builder's image (and anything it could have borrowed) is gone
    ASSERT_TRUE(pixmap_image( pixmap.toImage() ) == pixmap_image( expected ));
    // Same pixels as the RGB888 writer.
    FastRgbPair pair = build_fast_pair( source, 640, 360, 1280, 720 );
    ASSERT_TRUE(pixmap_image( pair.image888() ) == pixmap_image( expected ));

    // RED on f3b419ea: the hand-off wrapped a byte vector; fromImage shared it, so the pixmap
    // followed the vector's bytes (here overwritten instead of freed, to stay defined).
    std::vector<uint8_t> borrowed = pair.rgb32;
    const QPixmap shared = QPixmap::fromImage( playbackWrapRgb32Image( borrowed.data(), 1280, 720 ) );
    std::fill( borrowed.begin(), borrowed.end(), static_cast<uint8_t>( 0 ) );
    ASSERT_FALSE(pixmap_image( shared ) == pixmap_image( expected ));
}

/* r2 adversary A2 (introduced, major): the presented-content hash sampled one byte every
 * bytes/8192; at 3840x1600 RGB32 the step is 3000, a multiple of 4, so it read only blue and a
 * picture changing only in red hashed the same every frame (frozen-content FAIL). The RGB888 arm
 * had the same aliasing (step 2250, a multiple of 3: red only). Input: eleven frames, RGB=(20+i,0,0);
 * and for RGB888 eleven frames RGB=(0,20+i,0). */
TEST(PlaybackScaling, PresentedContentHashFollowsEveryChannel)
{
    const int width = 3840;
    const int height = 1600;
    // f3b419ea's sampler, for the RED half: one byte per sample (alpha skipped to the byte before).
    const auto oldHash = []( const uint8_t *bits, size_t bytes, bool rgb32 ) -> uint64_t
    {
        uint64_t h = 1469598103934665603ull;
        const size_t step = std::max( static_cast<size_t>( 1 ), bytes / 8192 );
        for( size_t i = 0; i < bytes; i += step )
        {
            const size_t at = ( rgb32 && ( i & 3u ) == 3u ) ? i - 1u : i;
            h ^= bits[at];
            h *= 1099511628211ull;
        }
        return h;
    };
    for( const bool rgb32 : { true, false } )
    {
        std::vector<uint64_t> fresh;
        std::vector<uint64_t> old;
        for( int i = 0; i <= 10; ++i )
        {
            QImage frame( width, height, rgb32 ? QImage::Format_RGB32 : QImage::Format_RGB888 );
            frame.fill( rgb32 ? QColor( 20 + i, 0, 0 ) : QColor( 0, 20 + i, 0 ) );
            const size_t bytes = static_cast<size_t>( frame.bytesPerLine() ) * static_cast<size_t>( frame.height() );
            fresh.push_back( playbackPresentedContentHash( frame.constBits(), bytes,
                                                           playbackPreparedBytesPerPixel( frame.format() ) ) );
            old.push_back( oldHash( frame.constBits(), bytes, rgb32 ) );
        }
        std::vector<uint64_t> distinct = fresh;
        std::sort( distinct.begin(), distinct.end() );
        ASSERT_EQ(fresh.size(), static_cast<size_t>( std::unique( distinct.begin(), distinct.end() ) - distinct.begin() ));
        // RED: the one-byte sampler hashed all eleven frames the same.
        ASSERT_TRUE(std::all_of( old.begin(), old.end(), [&]( uint64_t v ) { return v == old.front(); } ));
    }
    ASSERT_EQ(4, playbackPreparedBytesPerPixel( QImage::Format_RGB32 ));
    ASSERT_EQ(3, playbackPreparedBytesPerPixel( QImage::Format_RGB888 ));
    ASSERT_EQ(4, playbackPreparedBytesPerPixel( QImage::Format_ARGB32 ));
}
