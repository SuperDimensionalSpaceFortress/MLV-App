#ifndef PLAYBACKSCALING_H
#define PLAYBACKSCALING_H

#include <QImage>

#include <algorithm>
#include <array>
#include <cstdint>
#include <cmath>
#include <cstring>
#include <vector>

struct FastPlaybackScaleCache
{
    int sourceWidth = 0;
    int sourceHeight = 0;
    int targetWidth = 0;
    int targetHeight = 0;
    std::vector<int> xOffsets;
    std::vector<int> yOffsets;
};

/* Phase 4D: bilinear upscale cache. Stores per-output-row source-y indices
 * and per-output-column source-x indices plus 8-bit fractional weights
 * (0..256, where 256 means "right at the next sample"). Bilinear is the
 * preferred path when the render thread is upscaling Phase 4B's downsampled
 * output back to the display target — nearest-neighbour shows visible jaggies
 * and moire on diagonal edges, bilinear smooths them. */
struct BilinearPlaybackScaleCache
{
    int sourceWidth = 0;
    int sourceHeight = 0;
    int targetWidth = 0;
    int targetHeight = 0;
    /* For each output column x: x0SourceOffsets[x] = sourceX0 * 3 (RGB byte
     * offset of the left neighbour); xWeights[x] = (sourceX - sourceX0) * 256
     * clamped into [0, 256], stored as int16 so the multiply-add stays in
     * 16-bit integer space.  x1SourceOffsets[x] = sourceX1 * 3 where
     * sourceX1 = min(sourceWidth-1, sourceX0+1) (clamped at the right edge). */
    std::vector<int> x0SourceOffsets;
    std::vector<int> x1SourceOffsets;
    std::vector<int16_t> xWeights;
    /* For each output row y: y0RowOffsets[y] = sourceY0 * sourceWidth * 3;
     * y1RowOffsets[y] = sourceY1 * sourceWidth * 3 (sourceY1 clamped at the
     * bottom edge); yWeights[y] = (sourceY - sourceY0) * 256. */
    std::vector<int> y0RowOffsets;
    std::vector<int> y1RowOffsets;
    std::vector<int16_t> yWeights;
};

struct CubicPlaybackScaleCache
{
    int sourceWidth = 0;
    int sourceHeight = 0;
    int targetWidth = 0;
    int targetHeight = 0;
    std::vector<std::array<int, 4>> xSourceOffsets;
    std::vector<std::array<int, 4>> yRowOffsets;
    std::vector<std::array<float, 4>> xWeights;
    std::vector<std::array<float, 4>> yWeights;
};

enum class PlaybackPresentationScaleResampler
{
    Nearest,
    Bilinear,
    Cubic
};

inline PlaybackPresentationScaleResampler playbackChoosePresentationScaleResampler(
    int playbackScaleFactorActive,
    int phase4bPath,
    bool upscaling,
    bool presentationResize,
    bool aggressivePreviewActive)
{
    if( playbackScaleFactorActive <= 1 || !presentationResize )
    {
        return PlaybackPresentationScaleResampler::Nearest;
    }

    /* Aggressive preview is the user-facing coarse/fast mode. Keep
     * Sharp/Smooth on the anti-aliased paths, but let the x8 aggressive
     * lane use cubic on the final viewport resize so the tiny upstream
     * reduction does not explode into visible blockiness. */
    if( aggressivePreviewActive )
    {
        if( playbackScaleFactorActive >= 8 && upscaling )
        {
            return PlaybackPresentationScaleResampler::Cubic;
        }
        return PlaybackPresentationScaleResampler::Nearest;
    }

    if( playbackScaleFactorActive >= 4
     && phase4bPath == 0
     && upscaling )
    {
        return PlaybackPresentationScaleResampler::Cubic;
    }

    return PlaybackPresentationScaleResampler::Bilinear;
}

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: a recon texture is the reduced (x2/x4) route's when
 * it is smaller than the clip on both axes; x1, export, paused and scrub textures are
 * the clip's own size. Reduced textures get the AMaZE H-Nyquist filter. */
inline bool playbackReconTextureIsReduced(int textureWidth,
                                          int textureHeight,
                                          int clipWidth,
                                          int clipHeight)
{
    return textureWidth > 0 && textureHeight > 0
        && textureWidth < clipWidth && textureHeight < clipHeight;
}

/* The GPU window is handed the clip's DISPLAY size (clip x stretch, see
 * mainWindowGpuTexturePresentDisplaySize), not the clip size. Display stretch is >= 1 on
 * both axes (H 1-2, x3 for the 1/3 vertical setting; V 1, 1.667 or 3) and a reduced
 * texture is at most half the clip on each axis (+1 for odd dims), so the clip-sized
 * bound here is half the display size. A x1 clip stretched >= 2 on BOTH axes (H 2.0
 * with V 3.0) would read as reduced. */
inline bool playbackReconTextureIsReducedForDisplaySize(int textureWidth,
                                                        int textureHeight,
                                                        int displayWidth,
                                                        int displayHeight)
{
    if( displayWidth <= 0 || displayHeight <= 0 ) return false;
    return playbackReconTextureIsReduced(textureWidth,
                                         textureHeight,
                                         displayWidth / 2 + 2,
                                         displayHeight / 2 + 2);
}

inline bool playbackRgb8RowLooksLikeUniformTopMagentaBand(const uint8_t *row,
                                                          int width)
{
    if( !row || width <= 0 )
    {
        return false;
    }

    const int sampleStep = std::max( 1, width / 512 );
    int samples = 0;
    int magentaSamples = 0;
    int minR = 255, minG = 255, minB = 255;
    int maxR = 0, maxG = 0, maxB = 0;
    uint64_t sumR = 0, sumG = 0, sumB = 0;

    for( int x = 0; x < width; x += sampleStep )
    {
        const uint8_t *pixel = row + static_cast<size_t>( x ) * 3u;
        const int r = pixel[0];
        const int g = pixel[1];
        const int b = pixel[2];
        ++samples;
        sumR += static_cast<uint64_t>( r );
        sumG += static_cast<uint64_t>( g );
        sumB += static_cast<uint64_t>( b );
        minR = std::min( minR, r );
        minG = std::min( minG, g );
        minB = std::min( minB, b );
        maxR = std::max( maxR, r );
        maxG = std::max( maxG, g );
        maxB = std::max( maxB, b );
        if( r > 170
         && b > 140
         && g < 150
         && (((r + b) / 2) - g) > 35 )
        {
            ++magentaSamples;
        }
    }

    if( samples <= 0 )
    {
        return false;
    }

    const double invSamples = 1.0 / static_cast<double>( samples );
    const double avgR = static_cast<double>( sumR ) * invSamples;
    const double avgG = static_cast<double>( sumG ) * invSamples;
    const double avgB = static_cast<double>( sumB ) * invSamples;
    const bool magentaAverage =
        avgR >= 170.0
        && avgB >= 140.0
        && avgG <= 150.0
        && (avgR - avgG) >= 45.0
        && (avgB - avgG) >= 35.0;
    const bool mostlyMagenta =
        magentaSamples * 100 >= samples * 90;
    const bool lowVariance =
        (maxR - minR) <= 24
        && (maxG - minG) <= 80
        && (maxB - minB) <= 24;

    return magentaAverage && mostlyMagenta && lowVariance;
}

inline bool playbackRgb8RowLooksNearBlack(const uint8_t *row,
                                          int width)
{
    if( !row || width <= 0 )
    {
        return false;
    }

    const int sampleStep = std::max( 1, width / 512 );
    int samples = 0;
    int brightSamples = 0;
    uint64_t sumR = 0, sumG = 0, sumB = 0;

    for( int x = 0; x < width; x += sampleStep )
    {
        const uint8_t *pixel = row + static_cast<size_t>( x ) * 3u;
        const int r = pixel[0];
        const int g = pixel[1];
        const int b = pixel[2];
        ++samples;
        sumR += static_cast<uint64_t>( r );
        sumG += static_cast<uint64_t>( g );
        sumB += static_cast<uint64_t>( b );
        if( r > 8 || g > 8 || b > 8 )
        {
            ++brightSamples;
        }
    }

    if( samples <= 0 )
    {
        return false;
    }

    const double invSamples = 1.0 / static_cast<double>( samples );
    const double avg =
        (static_cast<double>( sumR + sumG + sumB ) * invSamples) / 3.0;
    return avg <= 4.0 && brightSamples * 100 <= samples * 2;
}

inline bool playbackRgb8RowLooksLikeUniformTopMagentaTransition(const uint8_t *row,
                                                                int width)
{
    if( !row || width <= 0 )
    {
        return false;
    }

    const int sampleStep = std::max( 1, width / 512 );
    int samples = 0;
    int magentaSamples = 0;
    int minR = 255, minG = 255, minB = 255;
    int maxR = 0, maxG = 0, maxB = 0;
    uint64_t sumR = 0, sumG = 0, sumB = 0;

    for( int x = 0; x < width; x += sampleStep )
    {
        const uint8_t *pixel = row + static_cast<size_t>( x ) * 3u;
        const int r = pixel[0];
        const int g = pixel[1];
        const int b = pixel[2];
        ++samples;
        sumR += static_cast<uint64_t>( r );
        sumG += static_cast<uint64_t>( g );
        sumB += static_cast<uint64_t>( b );
        minR = std::min( minR, r );
        minG = std::min( minG, g );
        minB = std::min( minB, b );
        maxR = std::max( maxR, r );
        maxG = std::max( maxG, g );
        maxB = std::max( maxB, b );
        if( r >= 70
         && b >= 55
         && g <= 110
         && (r - g) >= 18
         && (b - g) >= 10 )
        {
            ++magentaSamples;
        }
    }

    if( samples <= 0 )
    {
        return false;
    }

    const double invSamples = 1.0 / static_cast<double>( samples );
    const double avgR = static_cast<double>( sumR ) * invSamples;
    const double avgG = static_cast<double>( sumG ) * invSamples;
    const double avgB = static_cast<double>( sumB ) * invSamples;
    const bool magentaAverage =
        avgR >= 70.0
        && avgB >= 55.0
        && avgG <= 110.0
        && (avgR - avgG) >= 18.0
        && (avgB - avgG) >= 10.0;
    const bool mostlyMagenta =
        magentaSamples * 100 >= samples * 90;
    const bool lowVariance =
        (maxR - minR) <= 32
        && (maxG - minG) <= 32
        && (maxB - minB) <= 32;

    return magentaAverage && mostlyMagenta && lowVariance;
}

inline bool playbackRgb8RowLooksLikeTopMagentaEdgeAfterLetterbox(const uint8_t *row,
                                                                 int width)
{
    if( !row || width <= 0 )
    {
        return false;
    }

    const int sampleStep = std::max( 1, width / 512 );
    int samples = 0;
    int edgeSamples = 0;
    int brightSamples = 0;
    uint64_t sumR = 0, sumG = 0, sumB = 0;

    for( int x = 0; x < width; x += sampleStep )
    {
        const uint8_t *pixel = row + static_cast<size_t>( x ) * 3u;
        const int r = pixel[0];
        const int g = pixel[1];
        const int b = pixel[2];
        ++samples;
        sumR += static_cast<uint64_t>( r );
        sumG += static_cast<uint64_t>( g );
        sumB += static_cast<uint64_t>( b );
        if( r > 8 || g > 8 || b > 8 )
        {
            ++brightSamples;
        }
        if( r >= 65
         && b >= 50
         && g <= 115
         && (r - g) >= 18
         && (b - g) >= 8 )
        {
            ++edgeSamples;
        }
    }

    if( samples <= 0 )
    {
        return false;
    }

    const double invSamples = 1.0 / static_cast<double>( samples );
    const double avgR = static_cast<double>( sumR ) * invSamples;
    const double avgG = static_cast<double>( sumG ) * invSamples;
    const double avgB = static_cast<double>( sumB ) * invSamples;
    const bool magentaAverage =
        avgR >= 70.0
        && avgB >= 55.0
        && avgG <= 115.0
        && (avgR - avgG) >= 18.0
        && (avgB - avgG) >= 8.0;
    const bool enoughContent =
        brightSamples * 100 >= samples * 40;
    const bool enoughMagentaEdge =
        edgeSamples * 100 >= samples * 55;

    return magentaAverage && enoughContent && enoughMagentaEdge;
}

inline int playbackSuppressUniformTopMagentaBandRgb8(uint8_t *data,
                                                     int width,
                                                     int height,
                                                     int bytesPerLine = 0)
{
    if( !data || width <= 0 || height <= 0 )
    {
        return 0;
    }

    const int rowBytes = width * 3;
    const int stride = (bytesPerLine > 0) ? bytesPerLine : rowBytes;
    if( stride < rowBytes )
    {
        return 0;
    }

    const int maxProbeRows =
        std::min( height, std::max( 4, height / 8 ) );
    int probeStartRow = 0;
    for( ; probeStartRow < maxProbeRows; ++probeStartRow )
    {
        const uint8_t *row =
            data + static_cast<size_t>( probeStartRow ) * static_cast<size_t>( stride );
        if( !playbackRgb8RowLooksNearBlack( row, width ) )
        {
            break;
        }
    }
    if( probeStartRow >= maxProbeRows )
    {
        return 0;
    }

    int bandRows = 0;
    for( int y = probeStartRow; y < maxProbeRows; ++y )
    {
        const uint8_t *row =
            data + static_cast<size_t>( y ) * static_cast<size_t>( stride );
        if( !playbackRgb8RowLooksLikeUniformTopMagentaBand( row, width ) )
        {
            break;
        }
        ++bandRows;
    }

    if( bandRows < 4 )
    {
        if( probeStartRow >= 4 && bandRows == 0 )
        {
            int edgeRows = 0;
            for( int y = probeStartRow;
                 y < maxProbeRows && y < probeStartRow + 2;
                 ++y )
            {
                const uint8_t *row =
                    data + static_cast<size_t>( y ) * static_cast<size_t>( stride );
                if( !playbackRgb8RowLooksLikeTopMagentaEdgeAfterLetterbox( row, width ) )
                {
                    break;
                }
                ++edgeRows;
            }

            for( int y = probeStartRow; y < probeStartRow + edgeRows; ++y )
            {
                uint8_t *row =
                    data + static_cast<size_t>( y ) * static_cast<size_t>( stride );
                std::memset( row, 0, static_cast<size_t>( stride ) );
            }

            return edgeRows;
        }
        return 0;
    }

    int suppressRows = bandRows;
    for( int y = probeStartRow + bandRows;
         y < maxProbeRows && y < probeStartRow + bandRows + 2;
         ++y )
    {
        const uint8_t *row =
            data + static_cast<size_t>( y ) * static_cast<size_t>( stride );
        if( !playbackRgb8RowLooksLikeUniformTopMagentaTransition( row, width ) )
        {
            break;
        }
        ++suppressRows;
    }

    for( int y = probeStartRow; y < probeStartRow + suppressRows; ++y )
    {
        uint8_t *row =
            data + static_cast<size_t>( y ) * static_cast<size_t>( stride );
        std::memset( row, 0, static_cast<size_t>( stride ) );
    }

    return suppressRows;
}

inline float playbackCubicWeight(float x)
{
    const float a = -0.5f;
    x = std::fabs(x);
    if( x <= 1.0f )
    {
        return (a + 2.0f) * x * x * x
             - (a + 3.0f) * x * x
             + 1.0f;
    }
    if( x < 2.0f )
    {
        return a * x * x * x
             - 5.0f * a * x * x
             + 8.0f * a * x
             - 4.0f * a;
    }
    return 0.0f;
}

inline uint8_t playbackClampFloatToByte(float value)
{
    if( value <= 0.0f ) return 0;
    if( value >= 255.0f ) return 255;
    return static_cast<uint8_t>( value + 0.5f );
}

inline bool playbackBuildFastScaledRgb8(const uint8_t *source,
                                        int sourceWidth,
                                        int sourceHeight,
                                        int targetWidth,
                                        int targetHeight,
                                        std::vector<uint8_t> &scaledBuffer,
                                        FastPlaybackScaleCache &cache,
                                        int targetBytesPerLine = 0)
{
    if( !source
     || sourceWidth <= 0
     || sourceHeight <= 0
     || targetWidth <= 0
     || targetHeight <= 0 )
    {
        scaledBuffer.clear();
        return false;
    }

    const int rowBytes = targetWidth * 3;
    const int dstBytesPerLine =
        (targetBytesPerLine > 0) ? targetBytesPerLine : rowBytes;
    if( dstBytesPerLine < rowBytes )
    {
        scaledBuffer.clear();
        return false;
    }

    const size_t targetPixels =
        static_cast<size_t>( targetWidth ) * static_cast<size_t>( targetHeight );
    scaledBuffer.resize( static_cast<size_t>( dstBytesPerLine )
                         * static_cast<size_t>( targetHeight ) );

    if( cache.sourceWidth != sourceWidth
     || cache.sourceHeight != sourceHeight
     || cache.targetWidth != targetWidth
     || cache.targetHeight != targetHeight )
    {
        cache.sourceWidth = sourceWidth;
        cache.sourceHeight = sourceHeight;
        cache.targetWidth = targetWidth;
        cache.targetHeight = targetHeight;
        cache.xOffsets.resize( static_cast<size_t>( targetWidth ) );
        cache.yOffsets.resize( static_cast<size_t>( targetHeight ) );

        for( int x = 0; x < targetWidth; ++x )
        {
            const int srcX = std::min( sourceWidth - 1,
                                       static_cast<int>(
                                           ( static_cast<uint64_t>( x ) * static_cast<uint64_t>( sourceWidth ) )
                                           / static_cast<uint64_t>( targetWidth ) ) );
            cache.xOffsets[static_cast<size_t>( x )] = srcX * 3;
        }

        for( int y = 0; y < targetHeight; ++y )
        {
            const int srcY = std::min( sourceHeight - 1,
                                       static_cast<int>(
                                           ( static_cast<uint64_t>( y ) * static_cast<uint64_t>( sourceHeight ) )
                                           / static_cast<uint64_t>( targetHeight ) ) );
            cache.yOffsets[static_cast<size_t>( y )] = srcY * sourceWidth * 3;
        }
    }

    #pragma omp parallel for if(targetPixels >= 262144u)
    for( int y = 0; y < targetHeight; ++y )
    {
        const uint8_t *srcRow =
            source + static_cast<size_t>( cache.yOffsets[static_cast<size_t>( y )] );
        uint8_t *dstRow =
            scaledBuffer.data()
            + static_cast<size_t>( y ) * static_cast<size_t>( dstBytesPerLine );

        int x = 0;
        for( ; x + 3 < targetWidth; x += 4 )
        {
            const uint8_t *srcPixel0 =
                srcRow + static_cast<size_t>( cache.xOffsets[static_cast<size_t>( x )] );
            const uint8_t *srcPixel1 =
                srcRow + static_cast<size_t>( cache.xOffsets[static_cast<size_t>( x + 1 )] );
            const uint8_t *srcPixel2 =
                srcRow + static_cast<size_t>( cache.xOffsets[static_cast<size_t>( x + 2 )] );
            const uint8_t *srcPixel3 =
                srcRow + static_cast<size_t>( cache.xOffsets[static_cast<size_t>( x + 3 )] );
            uint8_t *dstPixel = dstRow + static_cast<size_t>( x ) * 3u;

            dstPixel[0] = srcPixel0[0];
            dstPixel[1] = srcPixel0[1];
            dstPixel[2] = srcPixel0[2];
            dstPixel[3] = srcPixel1[0];
            dstPixel[4] = srcPixel1[1];
            dstPixel[5] = srcPixel1[2];
            dstPixel[6] = srcPixel2[0];
            dstPixel[7] = srcPixel2[1];
            dstPixel[8] = srcPixel2[2];
            dstPixel[9] = srcPixel3[0];
            dstPixel[10] = srcPixel3[1];
            dstPixel[11] = srcPixel3[2];
        }

        for( ; x < targetWidth; ++x )
        {
            const uint8_t *srcPixel =
                srcRow + static_cast<size_t>( cache.xOffsets[static_cast<size_t>( x )] );
            uint8_t *dstPixel = dstRow + static_cast<size_t>( x ) * 3u;
            dstPixel[0] = srcPixel[0];
            dstPixel[1] = srcPixel[1];
            dstPixel[2] = srcPixel[2];
        }

        if( dstBytesPerLine > rowBytes )
        {
            std::fill( dstRow + rowBytes, dstRow + dstBytesPerLine, 0u );
        }
    }

    return true;
}

/* Phase 4D: bilinear RGB8 -> RGB8 scaler. Used by RenderFrameThread when the
 * render path produced a half-/quarter-resolution buffer (Phase 4B) and the
 * display target is larger.  Cache is populated on the first call for a given
 * source/target size combination and reused across frames; the heavy work is
 * the per-row sampling loop, which is OMP-parallel mirroring the existing
 * nearest-neighbour helper.
 *
 * The integer-math formulation uses 8-bit fractional weights (Q0.8) and keeps
 * the per-channel computation in 16-bit accumulators:
 *
 *   top    = src(x0,y0)*(256-fx) + src(x1,y0)*fx       // <= 65535, fits u16
 *   bottom = src(x0,y1)*(256-fx) + src(x1,y1)*fx       // <= 65535, fits u16
 *   out    = (top*(256-fy) + bottom*fy + 32768) >> 16  // round-to-nearest
 *
 * The +32768 / >>16 form is the standard Q0.8 dual-bilinear rounding
 * convention (top/bottom are already Q8.0 in 0..65535, the second weight pair
 * scales them to Q16.0 then we round-shift back to Q8.0).  This keeps
 * everything in 32-bit ints with no float ops per pixel. */
inline bool playbackBuildBilinearScaledRgb8(const uint8_t *source,
                                            int sourceWidth,
                                            int sourceHeight,
                                            int targetWidth,
                                            int targetHeight,
                                            std::vector<uint8_t> &scaledBuffer,
                                            BilinearPlaybackScaleCache &cache,
                                            int targetBytesPerLine = 0)
{
    if( !source
     || sourceWidth <= 0
     || sourceHeight <= 0
     || targetWidth <= 0
     || targetHeight <= 0 )
    {
        scaledBuffer.clear();
        return false;
    }

    const int rowBytes = targetWidth * 3;
    const int dstBytesPerLine =
        (targetBytesPerLine > 0) ? targetBytesPerLine : rowBytes;
    if( dstBytesPerLine < rowBytes )
    {
        scaledBuffer.clear();
        return false;
    }

    const size_t targetPixels =
        static_cast<size_t>( targetWidth ) * static_cast<size_t>( targetHeight );
    scaledBuffer.resize( static_cast<size_t>( dstBytesPerLine )
                         * static_cast<size_t>( targetHeight ) );

    if( cache.sourceWidth != sourceWidth
     || cache.sourceHeight != sourceHeight
     || cache.targetWidth != targetWidth
     || cache.targetHeight != targetHeight )
    {
        cache.sourceWidth = sourceWidth;
        cache.sourceHeight = sourceHeight;
        cache.targetWidth = targetWidth;
        cache.targetHeight = targetHeight;
        cache.x0SourceOffsets.resize( static_cast<size_t>( targetWidth ) );
        cache.x1SourceOffsets.resize( static_cast<size_t>( targetWidth ) );
        cache.xWeights.resize( static_cast<size_t>( targetWidth ) );
        cache.y0RowOffsets.resize( static_cast<size_t>( targetHeight ) );
        cache.y1RowOffsets.resize( static_cast<size_t>( targetHeight ) );
        cache.yWeights.resize( static_cast<size_t>( targetHeight ) );

        /* Map output sample x onto the source by aligning sample centres.  We
         * want srcCentreX = (x + 0.5) * sourceWidth / targetWidth - 0.5 so the
         * pixel grids line up; doing this in fixed-point Q0.8:
         *   numerator = (2*x + 1) * sourceWidth * 128 / targetWidth - 128
         * The result is an integer "256 * srcCentreX". */
        for( int x = 0; x < targetWidth; ++x )
        {
            int64_t numerator =
                static_cast<int64_t>( 2 * x + 1 ) * static_cast<int64_t>( sourceWidth ) * 128
                  / static_cast<int64_t>( targetWidth )
                - 128;
            int x0 = static_cast<int>( numerator >> 8 );
            int weight = static_cast<int>( numerator - ( static_cast<int64_t>( x0 ) << 8 ) );
            if( x0 < 0 )
            {
                x0 = 0;
                weight = 0;
            }
            int x1 = x0 + 1;
            if( x1 >= sourceWidth )
            {
                x1 = sourceWidth - 1;
                if( x0 >= sourceWidth )
                {
                    x0 = sourceWidth - 1;
                    weight = 0;
                }
            }
            cache.x0SourceOffsets[static_cast<size_t>( x )] = x0 * 3;
            cache.x1SourceOffsets[static_cast<size_t>( x )] = x1 * 3;
            cache.xWeights[static_cast<size_t>( x )] = static_cast<int16_t>( weight );
        }

        for( int y = 0; y < targetHeight; ++y )
        {
            int64_t numerator =
                static_cast<int64_t>( 2 * y + 1 ) * static_cast<int64_t>( sourceHeight ) * 128
                  / static_cast<int64_t>( targetHeight )
                - 128;
            int y0 = static_cast<int>( numerator >> 8 );
            int weight = static_cast<int>( numerator - ( static_cast<int64_t>( y0 ) << 8 ) );
            if( y0 < 0 )
            {
                y0 = 0;
                weight = 0;
            }
            int y1 = y0 + 1;
            if( y1 >= sourceHeight )
            {
                y1 = sourceHeight - 1;
                if( y0 >= sourceHeight )
                {
                    y0 = sourceHeight - 1;
                    weight = 0;
                }
            }
            cache.y0RowOffsets[static_cast<size_t>( y )] = y0 * sourceWidth * 3;
            cache.y1RowOffsets[static_cast<size_t>( y )] = y1 * sourceWidth * 3;
            cache.yWeights[static_cast<size_t>( y )] = static_cast<int16_t>( weight );
        }
    }

    #pragma omp parallel for if(targetPixels >= 262144u)
    for( int y = 0; y < targetHeight; ++y )
    {
        const uint8_t *rowTop =
            source + static_cast<size_t>( cache.y0RowOffsets[static_cast<size_t>( y )] );
        const uint8_t *rowBottom =
            source + static_cast<size_t>( cache.y1RowOffsets[static_cast<size_t>( y )] );
        const int fy = cache.yWeights[static_cast<size_t>( y )];
        const int fyComp = 256 - fy;
        uint8_t *dstRow =
            scaledBuffer.data()
            + static_cast<size_t>( y ) * static_cast<size_t>( dstBytesPerLine );

        for( int x = 0; x < targetWidth; ++x )
        {
            const int x0Off = cache.x0SourceOffsets[static_cast<size_t>( x )];
            const int x1Off = cache.x1SourceOffsets[static_cast<size_t>( x )];
            const int fx = cache.xWeights[static_cast<size_t>( x )];
            const int fxComp = 256 - fx;

            const uint8_t *p00 = rowTop + x0Off;
            const uint8_t *p10 = rowTop + x1Off;
            const uint8_t *p01 = rowBottom + x0Off;
            const uint8_t *p11 = rowBottom + x1Off;
            uint8_t *dstPixel = dstRow + static_cast<size_t>( x ) * 3u;

            /* Channel 0 */
            const int top0    = p00[0] * fxComp + p10[0] * fx;
            const int bottom0 = p01[0] * fxComp + p11[0] * fx;
            dstPixel[0] = static_cast<uint8_t>(
                ( top0 * fyComp + bottom0 * fy + 32768 ) >> 16 );

            /* Channel 1 */
            const int top1    = p00[1] * fxComp + p10[1] * fx;
            const int bottom1 = p01[1] * fxComp + p11[1] * fx;
            dstPixel[1] = static_cast<uint8_t>(
                ( top1 * fyComp + bottom1 * fy + 32768 ) >> 16 );

            /* Channel 2 */
            const int top2    = p00[2] * fxComp + p10[2] * fx;
            const int bottom2 = p01[2] * fxComp + p11[2] * fx;
            dstPixel[2] = static_cast<uint8_t>(
                ( top2 * fyComp + bottom2 * fy + 32768 ) >> 16 );
        }

        if( dstBytesPerLine > rowBytes )
        {
            std::fill( dstRow + rowBytes, dstRow + dstBytesPerLine, 0u );
        }
    }

    return true;
}

/* HQ playback upscale path. Catmull-Rom cubic interpolation keeps diagonals
 * and fine detail less blocky than bilinear when a very small x4 Dual ISO
 * preview is stretched to the viewport. It is intentionally used only for
 * high-quality upscales; Fast playback keeps the cheaper bilinear/nearest
 * paths. */
inline bool playbackBuildCubicScaledRgb8(const uint8_t *source,
                                         int sourceWidth,
                                         int sourceHeight,
                                         int targetWidth,
                                         int targetHeight,
                                         std::vector<uint8_t> &scaledBuffer,
                                         CubicPlaybackScaleCache &cache,
                                         int targetBytesPerLine = 0)
{
    if( !source
     || sourceWidth <= 0
     || sourceHeight <= 0
     || targetWidth <= 0
     || targetHeight <= 0 )
    {
        scaledBuffer.clear();
        return false;
    }

    const int rowBytes = targetWidth * 3;
    const int dstBytesPerLine =
        (targetBytesPerLine > 0) ? targetBytesPerLine : rowBytes;
    if( dstBytesPerLine < rowBytes )
    {
        scaledBuffer.clear();
        return false;
    }

    const size_t targetPixels =
        static_cast<size_t>( targetWidth ) * static_cast<size_t>( targetHeight );
    scaledBuffer.resize( static_cast<size_t>( dstBytesPerLine )
                         * static_cast<size_t>( targetHeight ) );

    if( cache.sourceWidth != sourceWidth
     || cache.sourceHeight != sourceHeight
     || cache.targetWidth != targetWidth
     || cache.targetHeight != targetHeight )
    {
        cache.sourceWidth = sourceWidth;
        cache.sourceHeight = sourceHeight;
        cache.targetWidth = targetWidth;
        cache.targetHeight = targetHeight;
        cache.xSourceOffsets.resize( static_cast<size_t>( targetWidth ) );
        cache.xWeights.resize( static_cast<size_t>( targetWidth ) );
        cache.yRowOffsets.resize( static_cast<size_t>( targetHeight ) );
        cache.yWeights.resize( static_cast<size_t>( targetHeight ) );

        for( int x = 0; x < targetWidth; ++x )
        {
            const float srcX =
                (static_cast<float>( x ) + 0.5f)
                * static_cast<float>( sourceWidth )
                / static_cast<float>( targetWidth )
                - 0.5f;
            const int baseX = static_cast<int>( std::floor( srcX ) );
            for( int tap = 0; tap < 4; ++tap )
            {
                const int sampleX = std::max( 0, std::min( sourceWidth - 1, baseX + tap - 1 ) );
                cache.xSourceOffsets[static_cast<size_t>( x )][static_cast<size_t>( tap )] =
                    sampleX * 3;
                cache.xWeights[static_cast<size_t>( x )][static_cast<size_t>( tap )] =
                    playbackCubicWeight( srcX - static_cast<float>( baseX + tap - 1 ) );
            }
        }

        for( int y = 0; y < targetHeight; ++y )
        {
            const float srcY =
                (static_cast<float>( y ) + 0.5f)
                * static_cast<float>( sourceHeight )
                / static_cast<float>( targetHeight )
                - 0.5f;
            const int baseY = static_cast<int>( std::floor( srcY ) );
            for( int tap = 0; tap < 4; ++tap )
            {
                const int sampleY = std::max( 0, std::min( sourceHeight - 1, baseY + tap - 1 ) );
                cache.yRowOffsets[static_cast<size_t>( y )][static_cast<size_t>( tap )] =
                    sampleY * sourceWidth * 3;
                cache.yWeights[static_cast<size_t>( y )][static_cast<size_t>( tap )] =
                    playbackCubicWeight( srcY - static_cast<float>( baseY + tap - 1 ) );
            }
        }
    }

    #pragma omp parallel for if(targetPixels >= 262144u)
    for( int y = 0; y < targetHeight; ++y )
    {
        uint8_t *dstRow =
            scaledBuffer.data()
            + static_cast<size_t>( y ) * static_cast<size_t>( dstBytesPerLine );
        const std::array<int, 4> &yOffsets = cache.yRowOffsets[static_cast<size_t>( y )];
        const std::array<float, 4> &wy = cache.yWeights[static_cast<size_t>( y )];

        for( int x = 0; x < targetWidth; ++x )
        {
            const std::array<int, 4> &xOffsets = cache.xSourceOffsets[static_cast<size_t>( x )];
            const std::array<float, 4> &wx = cache.xWeights[static_cast<size_t>( x )];
            float accumR = 0.0f;
            float accumG = 0.0f;
            float accumB = 0.0f;

            for( int yy = 0; yy < 4; ++yy )
            {
                const uint8_t *srcRow = source + static_cast<size_t>( yOffsets[static_cast<size_t>( yy )] );
                const float rowWeight = wy[static_cast<size_t>( yy )];
                for( int xx = 0; xx < 4; ++xx )
                {
                    const uint8_t *srcPixel =
                        srcRow + static_cast<size_t>( xOffsets[static_cast<size_t>( xx )] );
                    const float weight = rowWeight * wx[static_cast<size_t>( xx )];
                    accumR += static_cast<float>( srcPixel[0] ) * weight;
                    accumG += static_cast<float>( srcPixel[1] ) * weight;
                    accumB += static_cast<float>( srcPixel[2] ) * weight;
                }
            }

            uint8_t *dstPixel = dstRow + static_cast<size_t>( x ) * 3u;
            dstPixel[0] = playbackClampFloatToByte( accumR );
            dstPixel[1] = playbackClampFloatToByte( accumG );
            dstPixel[2] = playbackClampFloatToByte( accumB );
        }

        if( dstBytesPerLine > rowBytes )
        {
            std::fill( dstRow + rowBytes, dstRow + dstBytesPerLine, 0u );
        }
    }

    return true;
}

inline QImage playbackWrapRgb8Image(uint8_t *data, int width, int height, int bytesPerLine = 0)
{
    if( !data || width <= 0 || height <= 0 ) return QImage();
    // Many RGB8 buffers in this codebase are tightly packed at width*3
    // bytes/row, while render-thread playback-scaled buffers may be padded
    // to a 4-byte boundary. Qt's no-bytesPerLine constructor can compute an
    // aligned stride and lie about a tight buffer layout for any width%4 != 0,
    // leading to qt_convert_rgb888_to_rgb32_ssse3 reading past the buffer end
    // on the final row (SIGSEGV crash on 2026-04-24). Default the explicit
    // bytesPerLine to the packed value so the QImage tells Qt the truth;
    // callers with padded buffers must pass the padded stride explicitly.
    const int bpl = (bytesPerLine > 0) ? bytesPerLine : (width * 3);
    return QImage( data, width, height, bpl, QImage::Format_RGB888 );
}

inline QImage playbackBuildFastScaledImage(const uint8_t *source,
                                           int sourceWidth,
                                           int sourceHeight,
                                           int targetWidth,
                                           int targetHeight,
                                           std::vector<uint8_t> &scaledBuffer,
                                           FastPlaybackScaleCache &cache,
                                           int targetBytesPerLine = 0)
{
    if( !playbackBuildFastScaledRgb8( source,
                                      sourceWidth,
                                      sourceHeight,
                                      targetWidth,
                                      targetHeight,
                                      scaledBuffer,
                                      cache,
                                      targetBytesPerLine ) )
    {
        return QImage();
    }

    return playbackWrapRgb8Image( scaledBuffer.data(),
                                  targetWidth,
                                  targetHeight,
                                  targetBytesPerLine );
}

#endif // PLAYBACKSCALING_H
