/*!
* \file rbf_wrapper.cpp
* \author masc4ii
* \copyright 2019
* \brief Wrapper for rbfilter
*/

#include "rbf.h"
#include "rbf_wrapper.h"
#include "RBFilterPlain.h"
#include <atomic>
#include <cstdlib>

#ifdef __cplusplus
extern "C" {
#endif

namespace
{
thread_local CRBFilterPlain g_rbf_filter;

bool envFlagEnabled(const char * value)
{
    if( !value || !*value ) return false;
    return value[0] == '1'
        || value[0] == 'y'
        || value[0] == 'Y'
        || value[0] == 't'
        || value[0] == 'T'
        || value[0] == 'o'
        || value[0] == 'O';
}

bool recursiveBfDetailTimingEnabled()
{
    static const bool enabled =
        envFlagEnabled(std::getenv("MLVAPP_PLAYBACK_RBF_DETAIL_TIMING"));
    return enabled;
}

// -1: follow MLVAPP_RBF_SERIAL_VERTICAL; 0: serial; 1: parallel.
std::atomic<int> g_parallel_vertical_override(-1);

bool recursiveBfParallelVerticalEnabled()
{
    const int override_mode = g_parallel_vertical_override.load(std::memory_order_relaxed);
    if( override_mode >= 0 ) return override_mode != 0;
    static const bool serial =
        envFlagEnabled(std::getenv("MLVAPP_RBF_SERIAL_VERTICAL"));
    return !serial;
}

// Team size cap of the parallel filter. Playback runs it on the render thread
// while decode and dual-ISO recon run their own OpenMP teams. Uncapped (16
// threads) on Bachelor it cut the RBF to ~3.5 ms but tripled decode time and
// lost presented fps, so it is capped. MLVAPP_RBF_MAX_THREADS overrides
// (0: no cap). The picture does not depend on it.
const int kRecursiveBfDefaultMaxThreads = 4;

int recursiveBfMaxThreads()
{
    static const int max_threads = []() {
        const char * value = std::getenv("MLVAPP_RBF_MAX_THREADS");
        if( !value || !*value ) return kRecursiveBfDefaultMaxThreads;
        const int parsed = std::atoi(value);
        return parsed >= 0 ? parsed : kRecursiveBfDefaultMaxThreads;
    }();
    return max_threads;
}
}

void recursive_bf_set_parallel_vertical_override(int mode)
{
    g_parallel_vertical_override.store(mode < 0 ? -1 : (mode ? 1 : 0), std::memory_order_relaxed);
}

void recursive_bf_wrap_with_curve_index_lut(uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel,
        const uint16_t * output_lut,
        const int32_t * output_curve_r,
        const int32_t * output_curve_g,
        const int32_t * output_curve_b);

void recursive_bf_wrap_with_output_lut(uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel,
        const uint16_t * output_lut)
{
    recursive_bf_wrap_with_curve_index_lut(
        img_in,
        img_out,
        sigma_spatial,
        sigma_range,
        width,
        height,
        channel,
        output_lut,
        nullptr,
        nullptr,
        nullptr);
}

void recursive_bf_wrap_with_curve_index_lut(uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel,
        const uint16_t * output_lut,
        const int32_t * output_curve_r,
        const int32_t * output_curve_g,
        const int32_t * output_curve_b)
{
    if( 0 )
    {
        //Qingxiong Yang version
        recursive_bf(
            img_in,
            img_out,
            sigma_spatial*12.0f, sigma_range*4.0f/3.0f,
            width, height, channel,
            0);
    }
    else
    {
        //Ming version with better right boarder
        g_rbf_filter.reserveMemory( width, height, channel );
        g_rbf_filter.setTimingEnabled( recursiveBfDetailTimingEnabled() );
        g_rbf_filter.setParallelVertical( recursiveBfParallelVerticalEnabled() );
        g_rbf_filter.setMaxThreads( recursiveBfMaxThreads() );
        g_rbf_filter.filter( img_in,
                             img_out,
                             sigma_spatial,
                             sigma_range,
                             width,
                             height,
                             channel,
                             output_lut,
                             output_curve_r,
                             output_curve_g,
                             output_curve_b );
    }
}

void recursive_bf_wrap(uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel)
{
    recursive_bf_wrap_with_output_lut(
        img_in,
        img_out,
        sigma_spatial,
        sigma_range,
        width,
        height,
        channel,
        nullptr);
}

void recursive_bf_get_last_timing(recursive_bf_timing_t * timing)
{
    if( !timing ) return;

    const RBFilterPlainTiming last = g_rbf_filter.lastTiming();
    timing->total_ms = last.total_ms;
    timing->boundary_ms = last.boundary_ms;
    timing->range_table_ms = last.range_table_ms;
    timing->left_ms = last.left_ms;
    timing->right_ms = last.right_ms;
    timing->horizontal_average_ms = last.horizontal_average_ms;
    timing->vertical_down_ms = last.vertical_down_ms;
    timing->vertical_up_first_line_ms = last.vertical_up_first_line_ms;
    timing->vertical_up_body_ms = last.vertical_up_body_ms;
    timing->vertical_up_body_diff_ms = last.vertical_up_body_diff_ms;
    timing->vertical_up_body_store_ms = last.vertical_up_body_store_ms;
    timing->vertical_up_body_store_factor_ms = last.vertical_up_body_store_factor_ms;
    timing->vertical_up_body_store_color_ms = last.vertical_up_body_store_color_ms;
    timing->vertical_up_body_store_color_src_ms = last.vertical_up_body_store_color_src_ms;
    timing->vertical_up_body_store_color_prev_ms = last.vertical_up_body_store_color_prev_ms;
    timing->vertical_up_body_store_color_assign_ms = last.vertical_up_body_store_color_assign_ms;
    timing->vertical_up_ms = last.vertical_up_ms;
    timing->output_ms = last.output_ms;
}

#ifdef __cplusplus
}
#endif
