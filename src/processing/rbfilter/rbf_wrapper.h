/*!
* \file rbf_wrapper.h
* \author masc4ii
* \copyright 2019
* \brief Wrapper for rbfilter
*/

#ifndef RBF_WRAP_H
#define RBF_WRAP_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

typedef struct recursive_bf_timing_t
{
        double total_ms;
        double boundary_ms;
        double range_table_ms;
        double left_ms;
        double right_ms;
        double horizontal_average_ms;
        double vertical_down_ms;
        double vertical_up_first_line_ms;
        double vertical_up_body_ms;
        double vertical_up_body_diff_ms;
        double vertical_up_body_store_ms;
        double vertical_up_body_store_factor_ms;
        double vertical_up_body_store_color_ms;
        double vertical_up_body_store_color_src_ms;
        double vertical_up_body_store_color_prev_ms;
        double vertical_up_body_store_color_assign_ms;
        double vertical_up_ms;
        double output_ms;
} recursive_bf_timing_t;

extern void recursive_bf_wrap(
        uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel);

extern void recursive_bf_wrap_with_output_lut(
        uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel,
        const uint16_t * output_lut);

extern void recursive_bf_wrap_with_curve_index_lut(
        uint16_t * img_in,
        uint16_t * img_out,
        float sigma_spatial, float sigma_range,
        int width, int height, int channel,
        const uint16_t * output_lut,
        const int32_t * output_curve_r,
        const int32_t * output_curve_g,
        const int32_t * output_curve_b);

extern void recursive_bf_get_last_timing(recursive_bf_timing_t * timing);

/* The vertical passes run column-parallel, bit-exact to the legacy serial
 * passes. MLVAPP_RBF_SERIAL_VERTICAL=1 restores the serial passes (rollback).
 * mode -1 follows that env var, 0 forces serial, 1 forces parallel
 * (tests and benchmarks). */
extern void recursive_bf_set_parallel_vertical_override(int mode);

#ifdef __cplusplus
}
#endif

#endif // RBF_WRAP_H
