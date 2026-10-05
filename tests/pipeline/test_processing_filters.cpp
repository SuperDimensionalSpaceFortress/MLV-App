#include "../common/minitest.h"
#include "../common/hash_helpers.h"

#include "playback_path_test_state.h"

#include "../../src/processing/denoiser/denoiser_2d_median.h"
#include "../../src/processing/rbfilter/RBFilterPlain.h"
#include "../../src/processing/rbfilter/rbf_wrapper.h"
#include "../../src/processing/sobel/sobel.h"

#include <cstddef>
#include <cstdint>

extern "C" {
#include "../../src/mlv/llrawproc/pixelproc.h"
#include "../../src/mlv/llrawproc/patternnoise.h"
#include "../../src/processing/raw_processing.h"
}

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include <omp.h>

#ifdef _OPENMP
#include <omp.h>
#endif

namespace {

std::vector<uint16_t> make_rgb_pattern(int width, int height)
{
    std::vector<uint16_t> image(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3);
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const std::size_t index = (static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + static_cast<std::size_t>(x)) * 3;
            image[index + 0] = static_cast<uint16_t>((x * 173 + y * 97 + ((x ^ y) * 29)) % 65535);
            image[index + 1] = static_cast<uint16_t>((x * 41 + y * 211 + (x * y * 7)) % 65535);
            image[index + 2] = static_cast<uint16_t>((x * 19 + y * 59 + ((x + y) * 131)) % 65535);
        }
    }
    return image;
}

std::vector<uint16_t> make_bayer_pattern(int width, int height)
{
    std::vector<uint16_t> image(static_cast<std::size_t>(width) * static_cast<std::size_t>(height));
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const std::size_t index = static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + static_cast<std::size_t>(x);
            image[index] = static_cast<uint16_t>((2048 + x * 37 + y * 91 + ((x ^ y) * 13)) % 14000);
        }
    }
    return image;
}

std::string hash_image(const std::vector<uint16_t> & image)
{
    return sha256_bytes(image.data(), image.size() * sizeof(uint16_t));
}

void unset_env_for_test(const char * name)
{
#if defined(_WIN32)
    _putenv_s(name, "");
#else
    unsetenv(name);
#endif
}

void set_env_for_test(const char * name, const char * value)
{
#if defined(_WIN32)
    _putenv_s(name, value);
#else
    setenv(name, value, 1);
#endif
}

class ProcessingPlaybackPreviewModeScope
{
public:
    ProcessingPlaybackPreviewModeScope()
        : preview_(processingPlaybackPreviewModeEnabled())
        , aggressive_(processingPlaybackAggressivePreviewModeEnabled())
        , scale_factor_(processingPlaybackPreviewScaleFactor())
    {
    }

    ~ProcessingPlaybackPreviewModeScope()
    {
        processingSetPlaybackPreviewScaleFactor(scale_factor_);
        processingSetPlaybackAggressivePreviewMode(aggressive_);
        processingSetPlaybackPreviewMode(preview_);
    }

private:
    int preview_;
    int aggressive_;
    int scale_factor_;
};

#ifdef _OPENMP
class OpenMpThreadCountScope
{
public:
    explicit OpenMpThreadCountScope(int threads)
        : previous_(omp_get_max_threads())
    {
        omp_set_num_threads(threads);
    }

    ~OpenMpThreadCountScope()
    {
        omp_set_num_threads(previous_);
    }

private:
    int previous_;
};
#endif

uint16_t limit_u16_from_i32(std::int32_t value)
{
    if( value < 0 ) return 0;
    if( value > 65535 ) return 65535;
    return static_cast<uint16_t>(value);
}

int coerce_int(int value, int low, int high)
{
    return std::max(std::min(value, high), low);
}

int median5_copy(int values[5])
{
    int copy[5] = { values[0], values[1], values[2], values[3], values[4] };
    std::sort(copy, copy + 5);
    return copy[2];
}

void chroma_smooth_2x2_reference(std::vector<uint16_t> & image,
                                 int width,
                                 int height,
                                 int black,
                                 int white,
                                 int * raw2ev,
                                 int * ev2raw)
{
    constexpr int ev_resolution = 65536;
    const std::vector<uint16_t> input = image;

    for( int y = 4; y < height - 5; y += 2 )
    {
        for( int x = 4; x < width - 4; x += 2 )
        {
            int med_r[5];
            int med_b[5];
            int eh = 0;
            int ev = 0;

            const int sample_i[5] = { -2, 0, 0, 0, 2 };
            const int sample_j[5] = {  0, -2, 0, 2, 0 };

            for( int k = 0; k < 5; ++k )
            {
                const int base = (x + sample_i[k]) + (y + sample_j[k]) * width;
                const int r = input[base];
                const int b = input[base + 1 + width];
                const int g1 = raw2ev[input[base + 1]];
                const int g2 = raw2ev[input[base + width]];
                const int g3 = raw2ev[input[base - 1]];
                const int g5 = raw2ev[input[base + 2 + width]];
                const int gr = (g1 + g3) / 2;
                const int gb = (g2 + g5) / 2;
                eh += std::abs(g1 - g3) + std::abs(g2 - g5);
                med_r[k] = raw2ev[r] - gr;
                med_b[k] = raw2ev[b] - gb;
            }

            const int drh = median5_copy(med_r);
            const int dbh = median5_copy(med_b);

            for( int k = 0; k < 5; ++k )
            {
                const int base = (x + sample_i[k]) + (y + sample_j[k]) * width;
                const int r = input[base];
                const int b = input[base + 1 + width];
                const int g1 = raw2ev[input[base + 1]];
                const int g2 = raw2ev[input[base + width]];
                const int g4 = raw2ev[input[base - width]];
                const int g6 = raw2ev[input[base + 1 + 2 * width]];
                const int gr = (g2 + g4) / 2;
                const int gb = (g1 + g6) / 2;
                ev += std::abs(g2 - g4) + std::abs(g1 - g6);
                med_r[k] = raw2ev[r] - gr;
                med_b[k] = raw2ev[b] - gb;
            }

            const int drv = median5_copy(med_r);
            const int dbv = median5_copy(med_b);

            const int g1 = raw2ev[input[x + 1 + y * width]];
            const int g2 = raw2ev[input[x + (y + 1) * width]];
            const int g3 = raw2ev[input[x - 1 + y * width]];
            const int g4 = raw2ev[input[x + (y - 1) * width]];
            const int g5 = raw2ev[input[x + 2 + (y + 1) * width]];
            const int g6 = raw2ev[input[x + 1 + (y + 2) * width]];

            const int grv = (g2 + g4) / 2;
            const int grh = (g1 + g3) / 2;
            const int gbv = (g1 + g6) / 2;
            const int gbh = (g2 + g5) / 2;
            int gr = ev < eh ? grv : grh;
            int gb = ev < eh ? gbv : gbh;
            int dr = ev < eh ? drv : drh;
            int db = ev < eh ? dbv : dbh;

            const int r0 = input[x + y * width];
            const int b0 = input[x + 1 + (y + 1) * width];
            const int threshold = 64;
            if( r0 < black + threshold
             || b0 < black + threshold
             || std::abs(drv - drh) < threshold
             || std::abs(grv - grh) < threshold
             || std::abs(gbv - gbh) < threshold )
            {
                dr = (drv + drh) / 2;
                db = (dbv + dbh) / 2;
                gr = (g1 + g2 + g3 + g4) / 4;
                gb = (g1 + g2 + g5 + g6) / 4;
            }

            if( r0 < white )
            {
                image[x + y * width] =
                    static_cast<uint16_t>(ev2raw[coerce_int(gr + dr,
                                                            -10 * ev_resolution,
                                                            14 * ev_resolution - 1)]);
            }
            if( b0 < white )
            {
                image[x + 1 + (y + 1) * width] =
                    static_cast<uint16_t>(ev2raw[coerce_int(gb + db,
                                                            -10 * ev_resolution,
                                                            14 * ev_resolution - 1)]);
            }
        }
    }
}

} // namespace

TEST(ProcessingFilters, MedianDenoiserReuseMatchesFreshContext)
{
    const int width = 18;
    const int height = 14;

    std::vector<uint16_t> fresh = make_rgb_pattern(width, height);
    std::vector<uint16_t> reused = fresh;

    denoise_2d_median_context_t * context = nullptr;
    denoise_2D_median_with_context(reused.data(), width, height, 5, 65, &context);
    denoise_2D_median_with_context(fresh.data(), width, height, 5, 65, NULL);

    ASSERT_EQ(hash_image(fresh), hash_image(reused));

    denoise_2D_median_release(&context);
    ASSERT_TRUE(context == nullptr);
}

TEST(ProcessingFilters, RbfFilterReuseMatchesFreshResultAfterResize)
{
    const int small_width = 16;
    const int small_height = 12;
    std::vector<uint16_t> small_input = make_rgb_pattern(small_width, small_height);
    std::vector<uint16_t> expected_output(small_input.size(), 0);
    std::vector<uint16_t> actual_output(small_input.size(), 0);

    recursive_bf_wrap(small_input.data(), expected_output.data(), 0.0025f, 0.09f, small_width, small_height, 3);

    const int large_width = 28;
    const int large_height = 20;
    std::vector<uint16_t> large_input = make_rgb_pattern(large_width, large_height);
    std::vector<uint16_t> large_output(large_input.size(), 0);
    recursive_bf_wrap(large_input.data(), large_output.data(), 0.0025f, 0.09f, large_width, large_height, 3);

    recursive_bf_wrap(small_input.data(), actual_output.data(), 0.0025f, 0.09f, small_width, small_height, 3);

    ASSERT_EQ(hash_image(expected_output), hash_image(actual_output));
}

TEST(ProcessingFilters, RbfFilterReuseStaysStableAfterStateChanges)
{
    const int width = 20;
    const int height = 16;
    const float sigma_spatial = 0.0035f;
    const float sigma_range = 0.11f;

    std::vector<uint16_t> target_input = make_rgb_pattern(width, height);
    std::vector<uint16_t> expected_output(target_input.size(), 0);
    std::vector<uint16_t> first_actual_output(target_input.size(), 0);
    std::vector<uint16_t> second_actual_output(target_input.size(), 0);

    recursive_bf_wrap(target_input.data(),
                      expected_output.data(),
                      sigma_spatial,
                      sigma_range,
                      width,
                      height,
                      3);

    const int warm_width = 32;
    const int warm_height = 24;
    std::vector<uint16_t> warm_input = make_rgb_pattern(warm_width, warm_height);
    std::vector<uint16_t> warm_output(warm_input.size(), 0);
    recursive_bf_wrap(warm_input.data(), warm_output.data(), 0.0020f, 0.07f, warm_width, warm_height, 3);

    recursive_bf_wrap(target_input.data(),
                      first_actual_output.data(),
                      sigma_spatial,
                      sigma_range,
                      width,
                      height,
                      3);

    recursive_bf_wrap(target_input.data(),
                      second_actual_output.data(),
                      sigma_spatial,
                      sigma_range,
                      width,
                      height,
                      3);

    ASSERT_EQ(hash_image(expected_output), hash_image(first_actual_output));
    ASSERT_EQ(hash_image(expected_output), hash_image(second_actual_output));
}

TEST(ProcessingFilters, RbfFilterOutputLutMatchesSeparateLevelsPass)
{
    const int width = 22;
    const int height = 18;
    const float sigma_spatial = 0.0005f;
    const float sigma_range = 0.165f;

    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> expected_output(input.size(), 0);
    std::vector<uint16_t> actual_output(input.size(), 0);
    std::vector<uint16_t> levels_lut(65536);
    for( std::size_t i = 0; i < levels_lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        levels_lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
    }

    recursive_bf_wrap(input.data(),
                      expected_output.data(),
                      sigma_spatial,
                      sigma_range,
                      width,
                      height,
                      3);
    for( uint16_t & pixel : expected_output )
    {
        pixel = levels_lut[pixel];
    }

    recursive_bf_wrap_with_output_lut(input.data(),
                                      actual_output.data(),
                                      sigma_spatial,
                                      sigma_range,
                                      width,
                                      height,
                                      3,
                                      levels_lut.data());

    ASSERT_EQ(hash_image(expected_output), hash_image(actual_output));
}

TEST(ProcessingFilters, RbfFilterOutputLutStableAcrossOpenMpThreadCounts)
{
    const int width = 96;
    const int height = 64;
    const float sigma_spatial = 0.0025f;
    const float sigma_range = 0.165f;

    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> serial_output(input.size(), 0);
    std::vector<uint16_t> parallel_output(input.size(), 0);
    std::vector<uint16_t> levels_lut(65536);
    for( std::size_t i = 0; i < levels_lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        levels_lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
    }

#ifdef _OPENMP
    const int previous_threads = omp_get_max_threads();
    omp_set_num_threads(1);
#endif
    recursive_bf_wrap_with_output_lut(input.data(),
                                      serial_output.data(),
                                      sigma_spatial,
                                      sigma_range,
                                      width,
                                      height,
                                      3,
                                      levels_lut.data());

#ifdef _OPENMP
    omp_set_num_threads(4);
#endif
    recursive_bf_wrap_with_output_lut(input.data(),
                                      parallel_output.data(),
                                      sigma_spatial,
                                      sigma_range,
                                      width,
                                      height,
                                      3,
                                      levels_lut.data());

#ifdef _OPENMP
    omp_set_num_threads(previous_threads);
#endif

    ASSERT_EQ(hash_image(serial_output), hash_image(parallel_output));
}

TEST(ProcessingFilters, RbfFilterNoDetailFastPathMatchesTimedReference)
{
    const int width = 96;
    const int height = 64;
    const float sigma_spatial = 0.0025f;
    const float sigma_range = 0.165f;

    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> timed_output(input.size(), 0);
    std::vector<uint16_t> fast_output(input.size(), 0);
    std::vector<uint16_t> levels_lut(65536);
    for( std::size_t i = 0; i < levels_lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        levels_lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
    }

    CRBFilterPlain timed_filter;
    timed_filter.reserveMemory(width, height, 3);
    timed_filter.setTimingEnabled(true);
    timed_filter.filter(input.data(),
                        timed_output.data(),
                        sigma_spatial,
                        sigma_range,
                        width,
                        height,
                        3,
                        levels_lut.data());

    CRBFilterPlain fast_filter;
    fast_filter.reserveMemory(width, height, 3);
    fast_filter.setTimingEnabled(false);
    fast_filter.filter(input.data(),
                       fast_output.data(),
                       sigma_spatial,
                       sigma_range,
                       width,
                       height,
                       3,
                       levels_lut.data());

    ASSERT_EQ(hash_image(timed_output), hash_image(fast_output));
}

namespace {

/* Full 16-bit range noise over gradients and hard edges, so the range weights
 * vary from pixel to pixel and every chain of the vertical recursion differs. */
std::vector<uint16_t> make_rbf_stress_image(int width, int height, int channel, uint32_t seed)
{
    std::vector<uint16_t> image(static_cast<std::size_t>(width) * height * channel);
    uint32_t state = seed * 2654435761u + 12345u;
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            for( int c = 0; c < channel; ++c )
            {
                state = state * 1664525u + 1013904223u;
                const int noise = static_cast<int>(state >> 20) - 2048;
                const int edge = ((x / 7 + y / 5 + c) & 1) ? 52000 : 6000;
                const int gradient = (x * 65535) / std::max(1, width - 1) / 4 + (y * 65535) / std::max(1, height - 1) / 4;
                const std::size_t index = (static_cast<std::size_t>(y) * width + x) * channel + c;
                image[index] = limit_u16_from_i32((edge + gradient) / 2 + noise * ((x + y + c) % 3));
            }
        }
    }
    return image;
}

struct RbfRunResult
{
    std::vector<uint16_t> output;
    std::vector<float> down_color;
    std::vector<float> down_factor;
    std::vector<float> up_color;
    std::vector<float> up_factor;
};

RbfRunResult run_rbf_filter(bool parallel_vertical,
                            bool timing,
                            const std::vector<uint16_t> & input,
                            int width,
                            int height,
                            int channel,
                            const uint16_t * output_lut,
                            const int32_t * curve,
                            int max_threads = 0)
{
    std::vector<uint16_t> source = input;
    RbfRunResult result;
    result.output.assign(input.size(), 0);
    CRBFilterPlain filter;
    filter.reserveMemory(width, height, channel);
    filter.setTimingEnabled(timing);
    filter.setParallelVertical(parallel_vertical);
    filter.setMaxThreads(max_threads);
    filter.filter(source.data(),
                  result.output.data(),
                  0.0025f,
                  0.165f,
                  width,
                  height,
                  channel,
                  output_lut,
                  curve,
                  curve ? curve + 65536 : nullptr,
                  curve ? curve + 2 * 65536 : nullptr);
    const std::size_t pixels = static_cast<std::size_t>(width) * height;
    result.down_color.assign(filter.downPassColor(), filter.downPassColor() + pixels * channel);
    result.down_factor.assign(filter.downPassFactor(), filter.downPassFactor() + pixels);
    result.up_color.assign(filter.upPassColor(), filter.upPassColor() + pixels * channel);
    result.up_factor.assign(filter.upPassFactor(), filter.upPassFactor() + pixels);
    return result;
}

template <typename T>
std::size_t count_differing(const std::vector<T> & a, const std::vector<T> & b)
{
    if( a.size() != b.size() ) return a.size() + b.size();
    std::size_t differing = 0;
    for( std::size_t i = 0; i < a.size(); ++i )
    {
        if( std::memcmp(&a[i], &b[i], sizeof(T)) != 0 ) ++differing;
    }
    return differing;
}

} // namespace

TEST(ProcessingFilters, RbfFilterParallelVerticalMatchesSerialBitExact)
{
    /* The column-parallel vertical passes must reproduce the legacy serial
     * passes bit for bit: the raw float state of both passes and the uint16
     * output, on even and odd widths and heights, tall and wide shapes, both
     * channel layouts, every output mode, several thread counts, team caps
     * (setMaxThreads) and the timed path. */
    struct Size { int width; int height; };
    const Size sizes[] = {
        { 10, 10 }, { 11, 13 }, { 13, 11 }, { 17, 10 }, { 10, 37 }, { 37, 10 },
        { 64, 48 }, { 65, 47 }, { 223, 31 }, { 97, 129 }, { 452, 567 }, { 453, 569 },
    };
    std::vector<uint16_t> levels_lut(65536);
    for( std::size_t i = 0; i < levels_lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        levels_lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
    }
    std::vector<int32_t> curves(3 * 65536);
    for( std::size_t i = 0; i < 65536; ++i )
    {
        curves[i] = static_cast<int32_t>(i * 3 / 4);
        curves[65536 + i] = static_cast<int32_t>((i * 9) / 10 + 100);
        curves[2 * 65536 + i] = static_cast<int32_t>(65535 - i / 2);
    }
    const int thread_counts[] = { 1, 2, 3, 4, 7, 16 };

    int compared_runs = 0;
    for( const Size & size : sizes )
    {
        const bool large = size.width * size.height > 50000;
        for( int channel = 3; channel <= 4; ++channel )
        {
            const std::vector<uint16_t> input =
                make_rbf_stress_image(size.width, size.height, channel,
                                      static_cast<uint32_t>(size.width * 131 + size.height * 7 + channel));
            for( int mode = 0; mode < 3; ++mode )
            {
                if( mode == 2 && channel != 3 ) continue;
                if( large && mode == 1 ) continue;
                const uint16_t * lut = mode == 0 ? nullptr : levels_lut.data();
                const int32_t * curve = mode == 2 ? curves.data() : nullptr;

                /* With 4 channels the legacy horizontal average indexes pixels
                 * as i*3+c, so neighbouring pixels overlap and its result
                 * depends on thread timing. That pass is not touched here and
                 * every product caller uses 3 channels, so the 4-channel cases
                 * pin one thread to keep the horizontal input deterministic.
                 * filter() runs the legacy passes for 4 channels even when the
                 * parallel filter is requested (RbfParallelFilterIsRgb3Only),
                 * so these arms pin that equivalence, not a parallel race. */
                const int reference_threads = channel == 4 ? 1 : 4;
                RbfRunResult serial;
                {
#ifdef _OPENMP
                    OpenMpThreadCountScope threads(reference_threads);
#endif
                    serial = run_rbf_filter(false, false, input, size.width, size.height, channel, lut, curve);
                }
                /* Not vacuous: the filter changed the picture. */
                ASSERT_TRUE(count_differing(serial.output, input) > 0);

                for( int threads_count : thread_counts )
                {
                    if( large && threads_count != 1 && threads_count != 7 && threads_count != 16 ) continue;
                    if( channel == 4 && threads_count != 1 ) continue;
#ifdef _OPENMP
                    OpenMpThreadCountScope threads(threads_count);
#endif
                    for( int timing = 0; timing < 2; ++timing )
                    {
                        if( timing && threads_count != 7 && channel == 3 ) continue;
                        const RbfRunResult parallel =
                            run_rbf_filter(true, timing != 0, input, size.width, size.height, channel, lut, curve);
                        const std::size_t differing =
                            count_differing(serial.output, parallel.output)
                          + count_differing(serial.down_color, parallel.down_color)
                          + count_differing(serial.down_factor, parallel.down_factor)
                          + count_differing(serial.up_color, parallel.up_color)
                          + count_differing(serial.up_factor, parallel.up_factor);
                        if( differing != 0 )
                        {
                            std::fprintf(stderr,
                                         "[RBF-PARALLEL] %dx%d ch=%d mode=%d threads=%d timing=%d differing=%zu\n",
                                         size.width, size.height, channel, mode, threads_count, timing, differing);
                        }
                        ASSERT_EQ(static_cast<std::size_t>(0), differing);
                        ++compared_runs;
                    }

                    /* A team cap changes the column-block partition. */
                    if( channel != 3 || threads_count != 7 ) continue;
                    const int caps[] = { 1, 2, 3 };
                    for( int cap : caps )
                    {
                        const RbfRunResult capped =
                            run_rbf_filter(true, false, input, size.width, size.height, channel, lut, curve, cap);
                        const std::size_t differing =
                            count_differing(serial.output, capped.output)
                          + count_differing(serial.down_color, capped.down_color)
                          + count_differing(serial.down_factor, capped.down_factor)
                          + count_differing(serial.up_color, capped.up_color)
                          + count_differing(serial.up_factor, capped.up_factor);
                        if( differing != 0 )
                        {
                            std::fprintf(stderr,
                                         "[RBF-PARALLEL] %dx%d ch=%d mode=%d threads=%d cap=%d differing=%zu\n",
                                         size.width, size.height, channel, mode, threads_count, cap, differing);
                        }
                        ASSERT_EQ(static_cast<std::size_t>(0), differing);
                        ++compared_runs;
                    }
                }
            }
        }
    }
    ASSERT_TRUE(compared_runs > 100);
}

namespace {

/* Restores MLVAPP_RBF_PARALLEL, the wrapper's env cache and the override. */
class RbfParallelEnvScope
{
public:
    RbfParallelEnvScope()
    {
        const char * value = std::getenv(kName);
        had_value_ = value != nullptr;
        if( had_value_ ) value_ = value;
    }

    ~RbfParallelEnvScope()
    {
        if( had_value_ ) set_env_for_test(kName, value_.c_str());
        else unset_env_for_test(kName);
        recursive_bf_reset_parallel_env_cache_for_testing();
        recursive_bf_set_parallel_vertical_override(-1);
    }

    void set(const char * value)
    {
        if( value ) set_env_for_test(kName, value);
        else unset_env_for_test(kName);
        recursive_bf_reset_parallel_env_cache_for_testing();
        recursive_bf_set_parallel_vertical_override(-1);
    }

    static constexpr const char * kName = "MLVAPP_RBF_PARALLEL";

private:
    bool had_value_ = false;
    std::string value_;
};

/* Runs the product wrapper once and reports the dispatch it took. */
int rbf_wrapper_dispatch_parallel()
{
    const int width = 24;
    const int height = 16; /* reserveMemory needs >= 10 */
    std::vector<uint16_t> source = make_rbf_stress_image(width, height, 3, 11u);
    std::vector<uint16_t> output(source.size(), 0);
    recursive_bf_wrap(source.data(), output.data(), 0.0025f, 0.165f, width, height, 3);
    return recursive_bf_last_filter_was_parallel();
}

} // namespace

TEST(ProcessingFilters, RbfParallelEnvFlagIsExactAndCaseInsensitive)
{
    /* Only 1/true/yes/on (any case) opt in; "off" and malformed values such as
     * "1garbage" must keep the legacy passes. */
    struct Case { const char * value; int expected; };
    const Case cases[] = {
        { nullptr, 0 }, { "", 0 }, { "0", 0 }, { "off", 0 }, { "OFF", 0 }, { "Off", 0 },
        { "no", 0 }, { "NO", 0 }, { "false", 0 }, { "FALSE", 0 }, { "1garbage", 0 },
        { "trueish", 0 }, { "yess", 0 }, { "o", 0 }, { "t", 0 }, { "y", 0 }, { "2", 0 },
        { " 1", 0 }, { "1 ", 0 }, { "garbage", 0 },
        { "1", 1 }, { "true", 1 }, { "TRUE", 1 }, { "True", 1 }, { "yes", 1 }, { "YES", 1 },
        { "Yes", 1 }, { "on", 1 }, { "ON", 1 }, { "On", 1 },
    };
    RbfParallelEnvScope env;
    for( const Case & c : cases )
    {
        env.set(c.value);
        const int enabled = recursive_bf_parallel_enabled();
        if( enabled != c.expected )
        {
            std::fprintf(stderr, "[RBF-PARALLEL] MLVAPP_RBF_PARALLEL=%s -> %d, expected %d\n",
                         c.value ? c.value : "(unset)", enabled, c.expected);
        }
        ASSERT_EQ(c.expected, enabled);
    }
}

TEST(ProcessingFilters, RbfParallelFilterIsOptInByDefault)
{
    /* On Bachelor the parallel filter lowered render_work but lost presented
     * fps beside the overlapped CPU decode/recon, so the product default is
     * the legacy passes; MLVAPP_RBF_PARALLEL=1 opts in. This observes the
     * dispatch the wrapper actually took, so losing its setParallelVertical
     * call fails here whatever the class default is. */
    RbfParallelEnvScope env;
    env.set(nullptr);
    ASSERT_EQ(0, recursive_bf_parallel_enabled());
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());
    env.set("off");
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());
    env.set("1");
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());
    env.set(nullptr);
    recursive_bf_set_parallel_vertical_override(1);
    ASSERT_EQ(1, recursive_bf_parallel_enabled());
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());
    recursive_bf_set_parallel_vertical_override(0);
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());

    /* A filter that is never told to opt in runs the legacy passes. */
    std::vector<uint16_t> source = make_rbf_stress_image(24, 16, 3, 3u);
    std::vector<uint16_t> output(source.size(), 0);
    CRBFilterPlain filter;
    filter.reserveMemory(24, 16, 3);
    filter.filter(source.data(), output.data(), 0.0025f, 0.165f, 24, 16, 3);
    ASSERT_TRUE(!filter.lastFilterParallel());
    filter.setParallelVertical(true);
    filter.filter(source.data(), output.data(), 0.0025f, 0.165f, 24, 16, 3);
    ASSERT_TRUE(filter.lastFilterParallel());
}

TEST(ProcessingFilters, RbfParallelOverrideBeatsEnvBothWays)
{
    /* The override is how the bit-exact tests get their serial arm, so it must
     * win over MLVAPP_RBF_PARALLEL=1: mode 0 forces the legacy passes, mode 1
     * forces the parallel filter, mode -1 (and any negative) follows the env. */
    RbfParallelEnvScope env;
    env.set("1");
    ASSERT_EQ(1, recursive_bf_parallel_enabled());
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());

    recursive_bf_set_parallel_vertical_override(0);
    ASSERT_EQ(0, recursive_bf_parallel_enabled());
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());

    recursive_bf_set_parallel_vertical_override(-1);
    ASSERT_EQ(1, recursive_bf_parallel_enabled());
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());

    recursive_bf_set_parallel_vertical_override(0);
    recursive_bf_set_parallel_vertical_override(-7);
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());

    /* Override 1 wins over a disabling env too. */
    env.set("off");
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());
    recursive_bf_set_parallel_vertical_override(1);
    ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());
    recursive_bf_set_parallel_vertical_override(0);
    ASSERT_EQ(0, rbf_wrapper_dispatch_parallel());
}

TEST(ProcessingFilters, RbfParallelFilterIsRgb3Only)
{
    /* The parallel filter is bit-exact for 3 channels only. With 1 or 4 the
     * legacy indexing (i*3+c) overlaps or overruns and its result depends on
     * thread timing, so both the filter and the wrapper must keep the legacy
     * passes for them even when the parallel filter is enabled. */
#ifdef _OPENMP
    OpenMpThreadCountScope threads(1); /* the 4-channel legacy passes race */
#endif
    const int width = 24;
    const int height = 16;
    const int channels[] = { 1, 3, 4 };
    for( int channel : channels )
    {
        /* Legacy non-RGB3 passes index pixel i at i*3+c, so give every buffer
         * room for 3 floats/samples per pixel. */
        const int slots = std::max(channel, 3);
        std::vector<uint16_t> source = make_rbf_stress_image(width, height, slots, 5u + channel);
        std::vector<uint16_t> output(source.size(), 0);
        CRBFilterPlain filter;
        filter.reserveMemory(width, height * slots / channel, channel);
        filter.setParallelVertical(true);
        filter.filter(source.data(), output.data(), 0.0025f, 0.165f, width, height, channel);
        if( channel == 3 )
        {
            ASSERT_TRUE(filter.lastFilterParallel());
        }
        else
        {
            if( filter.lastFilterParallel() )
            {
                std::fprintf(stderr, "[RBF-PARALLEL] channel %d ran the parallel filter\n", channel);
            }
            ASSERT_TRUE(!filter.lastFilterParallel());
        }
    }

    /* Through the wrapper with the parallel filter enabled by env and by
     * override: RGB3 runs it, 4 channels keep the legacy passes. */
    RbfParallelEnvScope env;
    for( int forced = 0; forced < 2; ++forced )
    {
        env.set("1");
        if( forced ) recursive_bf_set_parallel_vertical_override(1);
        ASSERT_EQ(1, rbf_wrapper_dispatch_parallel());
        std::vector<uint16_t> source4 = make_rbf_stress_image(width, height, 4, 21u);
        std::vector<uint16_t> output4(source4.size(), 0);
        recursive_bf_wrap(source4.data(), output4.data(), 0.0025f, 0.165f, width, height, 4);
        ASSERT_EQ(0, recursive_bf_last_filter_was_parallel());
    }
}

TEST(ProcessingFilters, RbfLegacyOutputMatchesFrozenHashes)
{
    /* Frozen oracle for the shipped default: the differential tests compare the
     * legacy passes with the parallel ones, so a change to arithmetic they
     * share (e.g. getDiffFactorRgb3) would move both references unseen. These
     * hashes were recorded from the legacy passes at da8075d9. */
    struct Case { int width; int height; bool lut; const char * expected; };
    const Case cases[] = {
        { 64, 48, false, "02bceda852799a54875685f6457f66a01de51c239b442b35e950509ea6a67433" },
        { 65, 47, true, "891075248f1339a2313e6c521a9c6ce6051292ccf77eb7cdf6669491fdec1d15" },
    };
    std::vector<uint16_t> lut(65536);
    for( std::size_t i = 0; i < lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
    }
    for( const Case & c : cases )
    {
        const std::vector<uint16_t> input =
            make_rbf_stress_image(c.width, c.height, 3, static_cast<uint32_t>(c.width * 131 + c.height * 7 + 3));
        RbfRunResult result;
        {
#ifdef _OPENMP
            OpenMpThreadCountScope threads(1);
#endif
            result = run_rbf_filter(false, false, input, c.width, c.height, 3, c.lut ? lut.data() : nullptr, nullptr);
        }
        const std::string actual = hash_image(result.output);
        if( actual != c.expected )
        {
            std::fprintf(stderr, "[RBF-LEGACY] %dx%d lut=%d hash=%s expected=%s\n",
                         c.width, c.height, c.lut ? 1 : 0, actual.c_str(), c.expected);
        }
        ASSERT_EQ(std::string(c.expected), actual);
    }
}

TEST(ProcessingFilters, RbfParallelVerticalLeavesPlaybackShBlurUnchanged)
{
    /* Playback's S/H blur (the RBF on the standard x1 lane) must be
     * byte-identical with the serial vertical passes. The export path is proven
     * on real footage by ShFrameStateProxy.RbfParallelVerticalLeavesFixture-
     * ExportFramesUnchanged. */
    ProcessingPlaybackPreviewModeScope playback_scope;
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
    /* With MLVAPP_RBF_PARALLEL=1 exported the override must still pick the
     * arm, or the serial arm would compare parallel against parallel. */
    RbfParallelEnvScope env;
    env.set("1");

    const int width = 182;
    const int height = 121;
    const std::vector<uint16_t> input = make_rbf_stress_image(width, height, 3, 77u);

    int last_dispatch = -1;
    auto render_playback_blur = [&](int mode) {
        recursive_bf_set_parallel_vertical_override(mode);
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackAggressivePreviewMode(0);
        processingSetPlaybackPreviewScaleFactor(1);
        processingObject_t * processing = initProcessingObject();
        processingSetShadows(processing, 0.66);
        processingSetHighlights(processing, -0.26);
        std::vector<uint16_t> blur;
        std::vector<uint16_t> source = input;
        if( processingRefreshShadowsHighlightsBlurFromRgb16(processing, source.data(), width - 2, height - 1, 4, 0) )
        {
            const uint16_t * data = nullptr;
            int blur_width = 0;
            int blur_height = 0;
            if( processingGetShadowsHighlightsBlurData(processing, &data, &blur_width, &blur_height, nullptr) )
            {
                blur.assign(data, data + static_cast<std::size_t>(blur_width) * blur_height * 3u);
            }
        }
        last_dispatch = recursive_bf_last_filter_was_parallel();
        freeProcessingObject(processing);
        return blur;
    };

    std::vector<uint16_t> serial_blur;
    {
#ifdef _OPENMP
        OpenMpThreadCountScope threads(4);
#endif
        serial_blur = render_playback_blur(0);
    }
    ASSERT_TRUE(!serial_blur.empty());
    ASSERT_EQ(0, last_dispatch);

    const int thread_counts[] = { 1, 3, 8 };
    for( int threads_count : thread_counts )
    {
#ifdef _OPENMP
        OpenMpThreadCountScope threads(threads_count);
#endif
        ASSERT_EQ(hash_image(serial_blur), hash_image(render_playback_blur(1)));
        ASSERT_EQ(1, last_dispatch);
    }
    recursive_bf_set_parallel_vertical_override(-1);
}

TEST(ProcessingFilters, RbfParallelVerticalBenchmarkInformational)
{
    /* INFORMATIONAL micro-benchmark: serial vs column-parallel vertical passes
     * at the playback quarter-res S/H size (452x567, from 1808x2268) and at the
     * export full-res size. No duration is asserted; the numbers are
     * host-specific (the hub VM is CPU-contended). */
    struct Size { int width; int height; };
    const Size sizes[] = { { 452, 567 }, { 1808, 2268 } };
#ifdef _OPENMP
    /* The test runtime pins OMP_NUM_THREADS=1; measure with the host's threads. */
    OpenMpThreadCountScope host_threads(omp_get_num_procs());
#endif
    for( const Size & size : sizes )
    {
        const std::vector<uint16_t> input = make_rbf_stress_image(size.width, size.height, 3, 5u);
        std::vector<uint16_t> source = input;
        std::vector<uint16_t> output(input.size(), 0);
        CRBFilterPlain filter;
        filter.reserveMemory(size.width, size.height, 3);
        const int iterations = size.width > 1000 ? 5 : 15;
        auto median_filter_ms = [&](bool parallel, int threads_count) {
#ifdef _OPENMP
            OpenMpThreadCountScope threads(threads_count);
#endif
            filter.setParallelVertical(parallel);
            filter.setTimingEnabled(false);
            std::vector<double> total;
            for( int i = 0; i < iterations + 1; ++i )
            {
                const auto start = std::chrono::steady_clock::now();
                filter.filter(source.data(), output.data(), 0.0025f, 0.165f, size.width, size.height, 3);
                const double elapsed_ms = std::chrono::duration<double, std::milli>(
                    std::chrono::steady_clock::now() - start).count();
                if( i > 0 ) total.push_back(elapsed_ms); /* i == 0 warms up */
            }
            std::sort(total.begin(), total.end());
            return total[total.size() / 2];
        };
        const int host = std::max(1, omp_get_num_procs());
        const double serial_ms = median_filter_ms(false, host);
        const std::string serial_hash = hash_image(output);
        std::string scaling;
        std::vector<int> thread_counts;
        for( int threads_count = 1; threads_count < host; threads_count *= 2 ) thread_counts.push_back(threads_count);
        thread_counts.push_back(host);
        for( int threads_count : thread_counts )
        {
            const double parallel_ms = median_filter_ms(true, threads_count);
            /* The timed picture is the serial picture. */
            ASSERT_EQ(serial_hash, hash_image(output));
            char entry[64];
            std::snprintf(entry, sizeof(entry), " t%d=%.2f", threads_count, parallel_ms);
            scaling += entry;
        }

        /* Phase split of the parallel filter (omp_get_wtime, coarse on MinGW). */
        filter.setParallelVertical(true);
        filter.setTimingEnabled(true);
        filter.filter(source.data(), output.data(), 0.0025f, 0.165f, size.width, size.height, 3);
        const RBFilterPlainTiming timing = filter.lastTiming();
        std::printf("[RBF-VERTICAL-BENCH] %dx%d host_threads=%d filter ms (medians of %d): serial=%.2f parallel:%s | "
                    "parallel phases rows=%.1f columns(down+up+output)=%.1f\n",
                    size.width, size.height, host, iterations, serial_ms, scaling.c_str(),
                    timing.left_ms, timing.vertical_down_ms);
    }
}

TEST(ProcessingFilters, ShadowsHighlightsQuarterresBlurStableAcrossOpenMpThreadCounts)
{
    const int width = 192;
    const int height = 128;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);

    ProcessingPlaybackPreviewModeScope playback_scope;
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(0);
    processingSetPlaybackPreviewScaleFactor(1);
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();

    processingObject_t * serial_processing = initProcessingObject();
    processingObject_t * parallel_processing = initProcessingObject();
    ASSERT_TRUE(serial_processing != nullptr);
    ASSERT_TRUE(parallel_processing != nullptr);
    processingSetShadows(serial_processing, 0.66);
    processingSetHighlights(serial_processing, -0.26);
    processingSetShadows(parallel_processing, 0.66);
    processingSetHighlights(parallel_processing, -0.26);

    int serial_ok = 0;
    int parallel_ok = 0;
    const uint16_t * serial_blur = nullptr;
    const uint16_t * parallel_blur = nullptr;
    int serial_width = 0;
    int serial_height = 0;
    int parallel_width = 0;
    int parallel_height = 0;
    int serial_curve_index = 0;
    int parallel_curve_index = 0;

    {
#ifdef _OPENMP
        OpenMpThreadCountScope threads(1);
#endif
        serial_ok = processingRefreshShadowsHighlightsBlurFromRgb16(
            serial_processing, input.data(), width, height, 1, 0);
    }
    ASSERT_EQ(1, serial_ok);
    ASSERT_TRUE(processingGetShadowsHighlightsBlurData(serial_processing,
                                                       &serial_blur,
                                                       &serial_width,
                                                       &serial_height,
                                                       &serial_curve_index));

    {
#ifdef _OPENMP
        OpenMpThreadCountScope threads(4);
#endif
        parallel_ok = processingRefreshShadowsHighlightsBlurFromRgb16(
            parallel_processing, input.data(), width, height, 4, 0);
    }
    ASSERT_EQ(1, parallel_ok);
    ASSERT_TRUE(processingGetShadowsHighlightsBlurData(parallel_processing,
                                                       &parallel_blur,
                                                       &parallel_width,
                                                       &parallel_height,
                                                       &parallel_curve_index));

    ASSERT_EQ(serial_width, parallel_width);
    ASSERT_EQ(serial_height, parallel_height);
    ASSERT_EQ(serial_curve_index, parallel_curve_index);

    const std::size_t blur_words =
        static_cast<std::size_t>(serial_width) * static_cast<std::size_t>(serial_height) * 3u;
    std::vector<uint16_t> serial_copy(serial_blur, serial_blur + blur_words);
    std::vector<uint16_t> parallel_copy(parallel_blur, parallel_blur + blur_words);

    freeProcessingObject(serial_processing);
    freeProcessingObject(parallel_processing);

    ASSERT_EQ(hash_image(serial_copy), hash_image(parallel_copy));
}

TEST(ProcessingFilters, RbfFilterCurveIndexOutputMatchesSeparateLevelsAndMatrixPass)
{
    const int width = 22;
    const int height = 18;
    const float sigma_spatial = 0.0005f;
    const float sigma_range = 0.165f;

    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> filtered_rgb(input.size(), 0);
    std::vector<uint16_t> expected_output(input.size(), 0);
    std::vector<uint16_t> actual_output(input.size(), 0);
    std::vector<uint16_t> levels_lut(65536);
    std::vector<std::int32_t> curve_r(65536);
    std::vector<std::int32_t> curve_g(65536);
    std::vector<std::int32_t> curve_b(65536);
    for( std::size_t i = 0; i < levels_lut.size(); ++i )
    {
        const uint32_t value = static_cast<uint32_t>(i);
        levels_lut[i] = static_cast<uint16_t>((value * 257u + (value >> 3) + 19u) & 0xffffu);
        curve_r[i] = static_cast<std::int32_t>(i) - 5000;
        curve_g[i] = static_cast<std::int32_t>((i * 3u) / 4u);
        curve_b[i] = 8000 - static_cast<std::int32_t>(i / 2u);
    }

    recursive_bf_wrap_with_output_lut(input.data(),
                                      filtered_rgb.data(),
                                      sigma_spatial,
                                      sigma_range,
                                      width,
                                      height,
                                      3,
                                      levels_lut.data());
    for( std::size_t index = 0; index < filtered_rgb.size(); index += 3 )
    {
        const std::int32_t curve_index =
            ((curve_r[filtered_rgb[index + 0]] << 2)
           + (curve_g[filtered_rgb[index + 1]] * 11)
           +  curve_b[filtered_rgb[index + 2]]) >> 4;
        const uint16_t mask = limit_u16_from_i32(curve_index);
        expected_output[index + 0] = mask;
        expected_output[index + 1] = mask;
        expected_output[index + 2] = mask;
    }

    recursive_bf_wrap_with_curve_index_lut(input.data(),
                                           actual_output.data(),
                                           sigma_spatial,
                                           sigma_range,
                                           width,
                                           height,
                                           3,
                                           levels_lut.data(),
                                           curve_r.data(),
                                           curve_g.data(),
                                           curve_b.data());

    ASSERT_EQ(hash_image(expected_output), hash_image(actual_output));
}

TEST(ProcessingFilters, ShadowsHighlightsProbeTelemetryIsOptInByDefault)
{
    unset_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE");

    const int width = 24;
    const int height = 20;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> output(input.size(), 0);

    processingObject_t * processing = initProcessingObject();
    ASSERT_TRUE(processing != nullptr);
    processingSetShadows(processing, 0.30);
    processingSetHighlights(processing, -0.20);

    applyProcessingObject(processing,
                          width,
                          height,
                          input.data(),
                          output.data(),
                          2,
                          1,
                          0);

    /* PROD-TELEMETRY-DURATION-AS-PROOF-1: a Milliseconds() field can legitimately
     * read 0.0 when a stage runs faster than the timer resolves, so it must never
     * be the evidence for whether a stage ran. Assert on the execution counter /
     * completed-flag telemetry instead; durations stay reportable but unproven.
     * This fixture is 24x20 (even width and height), so the 16-bit pipeline takes
     * the halfres RBF lane by design (processing_compute_shadows_highlights_blur
     * halves even-dimension frames as a perf win); non-preview mode means the
     * quarterres preview lane never runs. */
    ASSERT_TRUE(processingGetLastShadowsHighlightsPrepRunCountForTesting() > 0);
    ASSERT_TRUE(processingGetLastShadowsHighlightsFilterRunCountForTesting() > 0);
    ASSERT_TRUE(processingGetLastShadowsHighlightsHalfresDownsampleCompletedForTesting());
    ASSERT_TRUE(processingGetLastShadowsHighlightsHalfresRbfCompletedForTesting());
    ASSERT_TRUE(processingGetLastShadowsHighlightsHalfresUpsampleCompletedForTesting());
    ASSERT_EQ(0, processingGetLastShadowsHighlightsQuarterresDownsampleCompletedForTesting());
    ASSERT_EQ(0, processingGetLastShadowsHighlightsQuarterresRbfCompletedForTesting());
    ASSERT_EQ(0, processingGetLastShadowsHighlightsQuarterresUpsampleCompletedForTesting());

    /* This is the test's namesake contract: with MLVAPP_SHADOWS_HIGHLIGHTS_PROBE
     * unset, processing_shadows_highlights_probe_mode() (via
     * processing_parse_probe_mode()) returns -1, so
     * shadows_highlights_probe_enabled in
     * processing_compute_shadows_highlights_blur() is false and every
     * per-substage omp_get_wtime() write - halfres AND quarterres alike - is
     * skipped (raw_processing.c: the `if( shadows_highlights_probe_enabled )`
     * guards around each _ms accumulator). These stay 0.0 because detailed probe
     * timing was never recorded, not because the halfres stages (proven above to
     * have run) were merely fast. Do not "fix" these back to run-proof duty -
     * that regression is what PROD-TELEMETRY-DURATION-AS-PROOF-1 round 3 undid. */
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresDownsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresRbfMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresUpsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresDownsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresRbfMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresUpsampleMilliseconds());

    freeProcessingObject(processing);
}

TEST(ProcessingFilters, SharpOddHeightPlaybackPreviewKeepsFullresShadowsHighlights)
{
    ProcessingPlaybackPreviewModeScope playback_scope;
    set_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE", "1");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(0);
    processingSetPlaybackPreviewScaleFactor(8);

    const int width = 48;
    const int height = 35;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> output(input.size(), 0);

    processingObject_t * processing = initProcessingObject();
    ASSERT_TRUE(processing != nullptr);
    processingSetShadows(processing, 0.30);
    processingSetHighlights(processing, -0.20);

    applyProcessingObject(processing,
                          width,
                          height,
                          input.data(),
                          output.data(),
                          2,
                          1,
                          0);

    /* PROD-TELEMETRY-DURATION-AS-PROOF-2: a fixture this small can run the
     * fullres RBF faster than omp_get_wtime() resolves, so Milliseconds() > 0.0
     * is not proof it ran -- assert the completed-flag set by the fullres
     * branch of processing_compute_shadows_highlights_blur() instead. */
    ASSERT_TRUE(processingGetLastShadowsHighlightsFullresCompletedForTesting());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresDownsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresRbfMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresUpsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresDownsampleMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresRbfMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresUpsampleMilliseconds());

    freeProcessingObject(processing);
    unset_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
}

TEST(ProcessingFilters, AggressiveOddHeightPlaybackPreviewUsesHalfresShadowsHighlights)
{
    ProcessingPlaybackPreviewModeScope playback_scope;
    set_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE", "1");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(1);
    processingSetPlaybackPreviewScaleFactor(4);

    const int width = 48;
    const int height = 35;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> output(input.size(), 0);

    processingObject_t * processing = initProcessingObject();
    ASSERT_TRUE(processing != nullptr);
    processingSetShadows(processing, 0.30);
    processingSetHighlights(processing, -0.20);

    applyProcessingObject(processing,
                          width,
                          height,
                          input.data(),
                          output.data(),
                          2,
                          1,
                          0);

    /* PROD-TELEMETRY-DURATION-AS-PROOF-2: proof by construction, not by a
     * wall-clock read that can round to 0.0 on a fast/tiny run -- the completed
     * flag and run counter are only ever set where the path actually executes. */
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterFullresMilliseconds());
    ASSERT_TRUE(processingGetLastShadowsHighlightsHalfresRbfCompletedForTesting());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterQuarterresRbfMilliseconds());
    ASSERT_TRUE(processingGetLastShadowsHighlightsFilterRunCountForTesting() > 0);

    freeProcessingObject(processing);
    unset_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
}

TEST(ProcessingFilters, AggressiveX8PlaybackPreviewUsesQuarterresShadowsHighlights)
{
    ProcessingPlaybackPreviewModeScope playback_scope;
    set_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE", "1");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(1);
    processingSetPlaybackPreviewScaleFactor(8);

    const int width = 226;
    const int height = 283;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> output(input.size(), 0);

    processingObject_t * processing = initProcessingObject();
    ASSERT_TRUE(processing != nullptr);
    processingSetShadows(processing, 0.30);
    processingSetHighlights(processing, -0.20);

    applyProcessingObject(processing,
                          width,
                          height,
                          input.data(),
                          output.data(),
                          2,
                          1,
                          0);

    /* PROD-TELEMETRY-DURATION-AS-PROOF-2: proof by construction, not by a
     * wall-clock read that can round to 0.0 on a fast/tiny run -- the completed
     * flags and run counter are only ever set where the path actually executes. */
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterFullresMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresRbfMilliseconds());
    ASSERT_TRUE(processingGetLastShadowsHighlightsQuarterresDownsampleCompletedForTesting());
    ASSERT_TRUE(processingGetLastShadowsHighlightsQuarterresRbfCompletedForTesting());
    ASSERT_TRUE(processingGetLastShadowsHighlightsQuarterresUpsampleCompletedForTesting());
    ASSERT_TRUE(processingGetLastShadowsHighlightsFilterRunCountForTesting() > 0);

    freeProcessingObject(processing);
    unset_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
}

TEST(ProcessingFilters, AggressiveX2PlaybackPreviewUsesQuarterresShadowsHighlights)
{
    ProcessingPlaybackPreviewModeScope playback_scope;
    unset_env_for_test("MLVAPP_DISABLE_AGGRESSIVE_X2_SH_QUARTERRES");
    set_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE", "1");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(1);
    processingSetPlaybackPreviewScaleFactor(2);
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();

    const int width = 226;
    const int height = 283;
    std::vector<uint16_t> input = make_rgb_pattern(width, height);
    std::vector<uint16_t> output(input.size(), 0);

    processingObject_t * processing = initProcessingObject();
    ASSERT_TRUE(processing != nullptr);
    processingSetShadows(processing, 0.30);
    processingSetHighlights(processing, -0.20);

    applyProcessingObject(processing,
                          width,
                          height,
                          input.data(),
                          output.data(),
                          1,
                          1,
                          0);

    std::fprintf(stderr,
                 "[AGGRESSIVE-X2-DIAG] path=%d cache=%d downsample_done=%d rbf_done=%d upsample_done=%d downsample_ms=%.6f rbf_ms=%.6f upsample_ms=%.6f total_ms=%.6f\n",
                 processingGetLastShadowsHighlightsQuarterresPathTakenForTesting(),
                 processingGetAggressiveX2ShadowsHighlightsQuarterresCacheForTesting(),
                 processingGetLastShadowsHighlightsQuarterresDownsampleCompletedForTesting(),
                 processingGetLastShadowsHighlightsQuarterresRbfCompletedForTesting(),
                 processingGetLastShadowsHighlightsQuarterresUpsampleCompletedForTesting(),
                 processingGetLastShadowsHighlightsFilterQuarterresDownsampleMilliseconds(),
                 processingGetLastShadowsHighlightsFilterQuarterresRbfMilliseconds(),
                 processingGetLastShadowsHighlightsFilterQuarterresUpsampleMilliseconds(),
                 processingGetLastShadowsHighlightsFilterMilliseconds());
    std::fprintf(stderr,
                 "[AGGRESSIVE-X2-CLOCK] source=omp_get_wtime omp_get_wtick_seconds=%.12f\n",
                 omp_get_wtick());

    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterFullresMilliseconds());
    ASSERT_EQ(0.0, processingGetLastShadowsHighlightsFilterHalfresRbfMilliseconds());
    ASSERT_EQ(1, processingGetLastShadowsHighlightsQuarterresPathTakenForTesting());
    ASSERT_EQ(1, processingGetLastShadowsHighlightsQuarterresDownsampleCompletedForTesting());
    ASSERT_EQ(1, processingGetLastShadowsHighlightsQuarterresRbfCompletedForTesting());
    ASSERT_EQ(1, processingGetLastShadowsHighlightsQuarterresUpsampleCompletedForTesting());

    freeProcessingObject(processing);
    unset_env_for_test("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE");
    processingResetShadowsHighlightsProbeModeCacheForTesting();
    processingResetShadowsHighlightsQuarterresEnvCacheForTesting();
}

TEST(ProcessingFilters, Exact2xRgbUpsampleMatchesGenericToSize)
{
    const int dimensions[][2] = {
        {1, 1},
        {2, 3},
        {17, 11},
        {33, 29},
    };

    for( const auto & dimensions_pair : dimensions )
    {
        const int width = dimensions_pair[0];
        const int height = dimensions_pair[1];
        const std::vector<uint16_t> input = make_rgb_pattern(width, height);
        ASSERT_TRUE(processingRgbU16UpsampleExact2xMatchesGenericForTesting(
            input.data(),
            width,
            height,
            2));
    }
}

TEST(ProcessingFilters, Exact4xRgbDownsampleMatchesTwoStep)
{
    const int dimensions[][2] = {
        {4, 4},
        {8, 12},
        {68, 44},
        {70, 58},
    };

    for( const auto & dimensions_pair : dimensions )
    {
        const int width = dimensions_pair[0];
        const int height = dimensions_pair[1];
        const std::vector<uint16_t> input = make_rgb_pattern(width, height);
        ASSERT_TRUE(processingRgbU16DownsampleExact4xMatchesTwoStepForTesting(
            input.data(),
            width,
            height,
            2));
    }
}

TEST(ProcessingFilters, ChromaSmooth2x2MatchesScalarReference)
{
    const int width = 34;
    const int height = 30;
    const int black = 256;
    const int white = 15000;

    std::vector<uint16_t> expected = make_bayer_pattern(width, height);
    std::vector<uint16_t> actual = expected;

    int * raw2ev = get_raw2ev(black);
    int * ev2raw = get_ev2raw(black);
    ASSERT_TRUE(raw2ev != nullptr);
    ASSERT_TRUE(ev2raw != nullptr);

    chroma_smooth_scratch_t scratch = { nullptr, 0 };
    chroma_smooth_2x2_reference(expected, width, height, black, white, raw2ev, ev2raw);
    chroma_smooth(2, actual.data(), width, height, black, white, raw2ev, ev2raw, &scratch);

    ASSERT_EQ(hash_image(expected), hash_image(actual));

    std::free(scratch.buffer);
    free_luts(raw2ev, ev2raw);
}

TEST(ProcessingFilters, SobelScratchReuseMatchesFreshResultAfterResize)
{
    const int target_width = 18;
    const int target_height = 14;
    const int warm_width = 30;
    const int warm_height = 22;

    std::vector<uint16_t> target_input = make_rgb_pattern(target_width, target_height);
    std::vector<uint16_t> warm_input = make_rgb_pattern(warm_width, warm_height);

    uint16_t * expected_gray = nullptr;
    uint16_t * expected_h = nullptr;
    uint16_t * expected_v = nullptr;
    uint16_t * expected_contour = nullptr;
    sobelFilter(target_input.data(),
                &expected_gray,
                &expected_h,
                &expected_v,
                &expected_contour,
                target_width,
                target_height);

    const std::size_t warm_pixels = static_cast<std::size_t>(warm_width) * static_cast<std::size_t>(warm_height);
    std::vector<uint16_t> reuse_gray(warm_pixels);
    std::vector<uint16_t> reuse_h(warm_pixels);
    std::vector<uint16_t> reuse_v(warm_pixels);
    std::vector<uint16_t> reuse_contour(warm_pixels);

    sobelFilterInto(warm_input.data(),
                    reuse_gray.data(),
                    reuse_h.data(),
                    reuse_v.data(),
                    reuse_contour.data(),
                    warm_width,
                    warm_height);

    sobelFilterInto(target_input.data(),
                    reuse_gray.data(),
                    reuse_h.data(),
                    reuse_v.data(),
                    reuse_contour.data(),
                    target_width,
                    target_height);

    const std::size_t target_pixels = static_cast<std::size_t>(target_width) * static_cast<std::size_t>(target_height);
    ASSERT_EQ(0, std::memcmp(expected_contour, reuse_contour.data(), target_pixels * sizeof(uint16_t)));

    std::free(expected_gray);
    std::free(expected_h);
    std::free(expected_v);
    std::free(expected_contour);
}

TEST(ProcessingFilters, PatternNoiseScratchReuseMatchesFreshResultAfterResize)
{
    const int target_width = 32;
    const int target_height = 24;
    const int warm_width = 48;
    const int warm_height = 36;
    const int white_level = 15000;

    std::vector<uint16_t> expected_input = make_bayer_pattern(target_width, target_height);
    std::vector<uint16_t> actual_input = expected_input;
    std::vector<uint16_t> warm_input = make_bayer_pattern(warm_width, warm_height);

    pattern_noise_scratch_t fresh_scratch = {};
    fix_pattern_noise(reinterpret_cast<int16_t *>(expected_input.data()),
                      target_width,
                      target_height,
                      white_level,
                      0,
                      &fresh_scratch);
    free_pattern_noise_scratch(&fresh_scratch);

    pattern_noise_scratch_t reused_scratch = {};
    fix_pattern_noise(reinterpret_cast<int16_t *>(warm_input.data()),
                      warm_width,
                      warm_height,
                      white_level,
                      0,
                      &reused_scratch);

    int16_t * full_res_block = reused_scratch.full_res_planes;
    int16_t * half_res_block = reused_scratch.half_res_planes;
    int * int_block = reused_scratch.int_scratch;

    fix_pattern_noise(reinterpret_cast<int16_t *>(actual_input.data()),
                      target_width,
                      target_height,
                      white_level,
                      0,
                      &reused_scratch);

    ASSERT_EQ(hash_image(expected_input), hash_image(actual_input));
    ASSERT_TRUE(reused_scratch.full_res_planes == full_res_block);
    ASSERT_TRUE(reused_scratch.half_res_planes == half_res_block);
    ASSERT_TRUE(reused_scratch.int_scratch == int_block);

    free_pattern_noise_scratch(&reused_scratch);
}

/* LOOK-ASSIST-GUI-FREEZE-1: the look-assist analysis clone must not pay for a
 * full default init, and the white-balance search must not print per candidate.
 * These count calls (processingDebug*Count); nothing here asserts wall-clock. */
namespace {

std::vector<uint16_t> make_flat_rgb16(int width, int height, uint16_t r, uint16_t g, uint16_t b)
{
    std::vector<uint16_t> image(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3);
    for( std::size_t i = 0; i < image.size(); i += 3 )
    {
        image[i + 0] = r;
        image[i + 1] = g;
        image[i + 2] = b;
    }
    return image;
}

std::vector<uint16_t> make_gradient_rgb16(int width, int height)
{
    std::vector<uint16_t> image(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3);
    for( int y = 0; y < height; ++y )
    {
        for( int x = 0; x < width; ++x )
        {
            const std::size_t index = (static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + static_cast<std::size_t>(x)) * 3;
            image[index + 0] = static_cast<uint16_t>(1500 + x * 1250 + y * 90);
            image[index + 1] = static_cast<uint16_t>(1200 + y * 1700 + x * 40);
            image[index + 2] = static_cast<uint16_t>(900 + (x + y) * 610);
        }
    }
    return image;
}

/* In the app processing->dual_iso points at the clip's llrawproc flag; init
 * leaves it NULL and highlight reconstruction dereferences it every frame. */
int g_look_assist_test_dual_iso = 1;

/* A fresh init has an all-zero camera matrix, which makes proper_wb_matrix NaN
 * and the white-balance search degenerate (delta 0 on the first candidate).
 * The app loads the clip's matrix; this is Canon EOS 5D Mark II (dcraw) as a
 * plausible stand-in. */
processingObject_t * init_with_camera_matrix()
{
    processingObject_t * p = initProcessingObject();
    if( !p ) return nullptr;
    double cam[9] = { 0.4716, 0.0603, -0.0830,
                     -0.7798, 1.5474,  0.2480,
                     -0.1496, 0.1937,  0.6651 };
    processingSetCamMatrix(p, cam, cam);
    return p;
}

processingObject_t * make_non_default_source()
{
    processingObject_t * src = init_with_camera_matrix();
    if( !src ) return nullptr;
    src->dual_iso = &g_look_assist_test_dual_iso;
    processingSetWhiteBalance(src, 4300.0, 2.5);
    processingSetExposureStops(src, 0.7);
    processingSetContrast(src, 0.6, 3.0, 0.4, 1.5, 0.1);
    processingSetPivot(src, 0.6);
    processingSetGamut(src, GAMUT_Rec2020);
    processingSetImageProfile(src, PROFILE_ALEXA_LOG);
    processingEnableHighlightReconstruction(src);
    return src;
}

int render_pair_memcmp(processingObject_t * a, processingObject_t * b)
{
    const int width = 48;
    const int height = 32;
    /* The levels pass rewrites its input in place, so each render gets its own copy. */
    std::vector<uint16_t> input_a = make_gradient_rgb16(width, height);
    std::vector<uint16_t> input_b = input_a;
    std::vector<uint16_t> out_a(input_a.size(), 0);
    std::vector<uint16_t> out_b(input_a.size(), 1);
    applyProcessingObject(a, width, height, input_a.data(), out_a.data(), 1, 1, 0);
    applyProcessingObject(b, width, height, input_b.data(), out_b.data(), 1, 1, 0);
    /* A flat or NaN-collapsed render would make the comparison vacuous. */
    if( std::count(out_a.begin(), out_a.end(), out_a[0]) == static_cast<std::ptrdiff_t>(out_a.size()) )
    {
        return 2;
    }
    return std::memcmp(out_a.data(), out_b.data(), out_a.size() * sizeof(uint16_t));
}

} // namespace

TEST(LookAssistClone, CloneDoesNotRunFullDefaultInit)
{
    processingObject_t * src = make_non_default_source();
    ASSERT_TRUE(src != nullptr);

    /* Positive control: the counter really moves on a full init. */
    const unsigned long before_init = processingDebugFullInitCount();
    processingObject_t * probe = initProcessingObject();
    ASSERT_TRUE(probe != nullptr);
    ASSERT_EQ(1ul, processingDebugFullInitCount() - before_init);
    freeProcessingObject(probe);

    const unsigned long before_clone = processingDebugFullInitCount();
    processingObject_t * clone = processingCloneForAnalysis(src);
    ASSERT_TRUE(clone != nullptr);
    ASSERT_EQ(0ul, processingDebugFullInitCount() - before_clone);

    processingFreeClone(clone);
    freeProcessingObject(src);
}

TEST(LookAssistClone, CloneRendersIdenticallyAndOwnsItsBuffers)
{
    processingObject_t * src = make_non_default_source();
    ASSERT_TRUE(src != nullptr);
    /* The source's transfer function must differ from init's default, or the
     * clone could pass by accident on the default it used to get from init. */
    ASSERT_TRUE(src->transfer_function_string != nullptr);
    ASSERT_TRUE(std::strcmp(src->transfer_function_string, "pow(x/(1+x), 1/3.15)") != 0);
    processingObject_t * clone = processingCloneForAnalysis(src);
    ASSERT_TRUE(clone != nullptr);

    /* Structure first: a clone that shares or lacks a buffer must fail here, cleanly,
     * rather than crash inside a render. */
    ASSERT_TRUE(clone->filter != src->filter);
    ASSERT_TRUE(clone->lut != src->lut);
    for( int i = 0; i < 9; ++i )
    {
        ASSERT_TRUE(clone->pre_calc_matrix[i] != nullptr);
        ASSERT_TRUE(clone->pre_calc_matrix[i] != src->pre_calc_matrix[i]);
        ASSERT_TRUE(clone->pre_calc_matrix_gradient[i] != nullptr);
        ASSERT_TRUE(clone->pre_calc_matrix_gradient[i] != src->pre_calc_matrix_gradient[i]);
    }
    for( int i = 0; i < 7; ++i )
    {
        ASSERT_TRUE(clone->cs_zone.pre_calc_rgb_to_YCbCr[i] != nullptr);
        ASSERT_TRUE(clone->cs_zone.pre_calc_rgb_to_YCbCr[i] != src->cs_zone.pre_calc_rgb_to_YCbCr[i]);
    }
    for( int i = 0; i < 4; ++i )
    {
        ASSERT_TRUE(clone->cs_zone.pre_calc_YCbCr_to_rgb[i] != nullptr);
        ASSERT_TRUE(clone->cs_zone.pre_calc_YCbCr_to_rgb[i] != src->cs_zone.pre_calc_YCbCr_to_rgb[i]);
    }
    ASSERT_TRUE(clone->cs_zone.pre_calc_YCbCr_to_rgb[4] == nullptr);
    ASSERT_TRUE(clone->shadows_highlights.blur_image != src->shadows_highlights.blur_image);
    ASSERT_TRUE(clone->shadows_highlights.blur_image_half_in != src->shadows_highlights.blur_image_half_in);
    ASSERT_TRUE(clone->shadows_highlights.blur_image_half_out != src->shadows_highlights.blur_image_half_out);
    ASSERT_TRUE(clone->transfer_function != nullptr);
    ASSERT_TRUE(clone->transfer_function_string != nullptr);
    ASSERT_TRUE(clone->transfer_function_string_formatted != nullptr);
    ASSERT_TRUE(clone->transfer_function != src->transfer_function);
    ASSERT_TRUE(clone->transfer_function_string != src->transfer_function_string);
    ASSERT_TRUE(clone->transfer_function_string_formatted != src->transfer_function_string_formatted);
    ASSERT_EQ(0, std::strcmp(clone->transfer_function_string, src->transfer_function_string));

    ASSERT_EQ(0, render_pair_memcmp(src, clone));

    /* Same setters on both: exercises the clone's own compiled transfer function. */
    processingSetWhiteBalance(src, 5100.0, -1.3);
    processingSetWhiteBalance(clone, 5100.0, -1.3);
    processingSetExposureStops(src, -0.4);
    processingSetExposureStops(clone, -0.4);
    processingSetGamut(src, GAMUT_AdobeRGB);
    processingSetGamut(clone, GAMUT_AdobeRGB);
    ASSERT_EQ(0, render_pair_memcmp(src, clone));

    processingFreeClone(clone);
    freeProcessingObject(src);
}

/* Flat synthetic patches in the 16-bit domain (pre_calc_levels maps 8192..59580 to 0..65535). */
#define PATCH_WARM_R 44000
#define PATCH_WARM_G 36000
#define PATCH_WARM_B 24000
#define PATCH_COOL_R 30000
#define PATCH_COOL_G 36000
#define PATCH_COOL_B 32000

TEST(WhiteBalanceSearch, SearchDoesNotPrintPerCandidate)
{
    processingObject_t * processing = init_with_camera_matrix();
    ASSERT_TRUE(processing != nullptr);
    std::vector<uint16_t> image = make_flat_rgb16(64, 64, PATCH_WARM_R, PATCH_WARM_G, PATCH_WARM_B);

    int temp = 0;
    int tint = 0;
    const unsigned long before = processingDebugFinalMatrixPrintCount();
    processingFindWhiteBalance(processing, 64, 64, image.data(), 32, 32, &temp, &tint, 0);
    /* The search must have walked past the first candidate, otherwise this
     * test would pass for the wrong reason. */
    ASSERT_TRUE(temp != 2300 || tint != -100);
    /* Only the restore of the original white balance may print. */
    ASSERT_EQ(1ul, processingDebugFinalMatrixPrintCount() - before);

    freeProcessingObject(processing);
}

TEST(WhiteBalanceSearch, OrdinaryUpdatesStillPrintAndSolverOutputPinned)
{
    processingObject_t * processing = init_with_camera_matrix();
    ASSERT_TRUE(processing != nullptr);
    const unsigned long before = processingDebugFinalMatrixPrintCount();
    processingSetWhiteBalance(processing, 5000.0, 0.0);
    const unsigned long plain_delta = processingDebugFinalMatrixPrintCount() - before;
    ASSERT_EQ(1ul, plain_delta);
    freeProcessingObject(processing);

    /* Solver results captured on the pre-fix (C1) build. */
    struct Patch { uint16_t r, g, b; int temp, tint; };
    const Patch patches[2] = {
        { PATCH_WARM_R, PATCH_WARM_G, PATCH_WARM_B, 2300, -73 },  /* warm cast */
        { PATCH_COOL_R, PATCH_COOL_G, PATCH_COOL_B, 3840, -94 },  /* cool / green cast */
    };
    for( const Patch & patch : patches )
    {
        processingObject_t * p = init_with_camera_matrix();
        ASSERT_TRUE(p != nullptr);
        std::vector<uint16_t> image = make_flat_rgb16(64, 64, patch.r, patch.g, patch.b);
        int temp = 0;
        int tint = 0;
        processingFindWhiteBalance(p, 64, 64, image.data(), 32, 32, &temp, &tint, 0);
        ASSERT_EQ(patch.temp, temp);
        ASSERT_EQ(patch.tint, tint);
        freeProcessingObject(p);
    }
}
