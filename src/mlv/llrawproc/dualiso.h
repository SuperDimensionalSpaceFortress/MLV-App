/*
 * Copyright (C) 2014 David Milligan
 * Copyright (C) 2017 bouncyball
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the
 * Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor,
 * Boston, MA  02110-1301, USA.
 */

#ifndef _dualiso_h
#define _dualiso_h

#include <sys/types.h>
#include <stdint.h>
#include "../raw.h"

#if defined(_MSC_VER)
#define DUALISO_THREAD_LOCAL __declspec(thread)
#else
#define DUALISO_THREAD_LOCAL __thread
#endif

enum
{
    DUALISO_MIX_CURVE_CACHE_SLOTS = 4
};

enum
{
    DUALISO_FULL20_PATH_NONE = 0,
    DUALISO_FULL20_PATH_CPU_FULL20 = 1,
    DUALISO_FULL20_PATH_GPU_PREPARE = 2
};

enum
{
    DUALISO_FULL20_PATTERN_SOURCE_NONE = 0,
    DUALISO_FULL20_PATTERN_SOURCE_FRESH_AUTO = 1,
    DUALISO_FULL20_PATTERN_SOURCE_EXPLICIT_POSITIVE = 2,
    DUALISO_FULL20_PATTERN_SOURCE_CACHED_NEGATIVE = 3,
    DUALISO_FULL20_PATTERN_SOURCE_CACHED_NEGATIVE_REDETECTED = 4,
    DUALISO_FULL20_PATTERN_SOURCE_SPECIAL_AUTO_DETECT = 5,
    DUALISO_FULL20_PATTERN_SOURCE_SPECIAL_AUTO_DEFAULT = 6,
    DUALISO_FULL20_PATTERN_SOURCE_INVALID = 7
};

typedef struct
{
    struct histogram * histograms[4];
    int * data_x;
    int * data_y;
    double * data_w;
    size_t data_capacity;
    uint16_t * output_image;
    size_t output_capacity;
    double last_histogram_ms;
    double last_regression_ms;
    double last_rowscale_ms;
} dualiso_preview_scratch_t;

typedef struct
{
    uint32_t * raw_buffer_32;
    uint32_t * dark;
    uint32_t * bright;
    uint32_t * fullres;
    uint32_t * halfres;
    uint32_t * fullres_smooth;
    uint32_t * halfres_smooth;
    uint16_t * overexposed;
    uint16_t * alias_map;
    uint16_t * over_aux;
    int * ev_raw2ev;
    float * ev_raw2ev_float;
    int * ev2raw_0;
    double * mix_curve[DUALISO_MIX_CURVE_CACHE_SLOTS];
    float * mix_curve_float[DUALISO_MIX_CURVE_CACHE_SLOTS];
    size_t pixel_capacity;
    size_t ev_raw2ev_capacity;
    size_t ev_raw2ev_float_capacity;
    size_t ev2raw_capacity;
    int ev_lut_valid;
    int ev_lut_float_valid;
    int ev_lut_black;
    int ev_lut_white;
    size_t mix_curve_capacity[DUALISO_MIX_CURVE_CACHE_SLOTS];
    size_t mix_curve_float_capacity[DUALISO_MIX_CURVE_CACHE_SLOTS];
    int mix_curve_valid[DUALISO_MIX_CURVE_CACHE_SLOTS];
    int mix_curve_float_valid[DUALISO_MIX_CURVE_CACHE_SLOTS];
    uint32_t mix_curve_last_black[DUALISO_MIX_CURVE_CACHE_SLOTS];
    uint32_t mix_curve_last_white[DUALISO_MIX_CURVE_CACHE_SLOTS];
    double mix_curve_last_corr_ev[DUALISO_MIX_CURVE_CACHE_SLOTS];
    double mix_curve_last_overlap[DUALISO_MIX_CURVE_CACHE_SLOTS];
    uint64_t mix_curve_last_used[DUALISO_MIX_CURVE_CACHE_SLOTS];
    uint64_t mix_curve_use_counter;
    double * fullres_curve;
    float * fullres_curve_float;
    double * fullres_curve_float_as_double;
    size_t fullres_curve_capacity;
    size_t fullres_curve_float_capacity;
    size_t fullres_curve_float_as_double_capacity;
    int fullres_curve_valid;
    int fullres_curve_float_valid;
    int fullres_curve_float_as_double_valid;
    int fullres_curve_black;
    int fullres_curve_float_black;
    int fullres_curve_float_as_double_black;

    int * histogram_match_dark;
    int * histogram_match_bright;
    int * histogram_match_tmp;
    int * histogram_match_hi_dark;
    int * histogram_match_hi_bright;
    size_t histogram_match_pixel_capacity;
    size_t histogram_match_sample_capacity;
    size_t histogram_match_highlight_capacity;

    int * identify_histograms;
    size_t identify_histogram_capacity;

    int * amaze_squeezed;
    float ** amaze_rawData_rows;
    float ** amaze_red_rows;
    float ** amaze_green_rows;
    float ** amaze_blue_rows;
    float * amaze_rawData_storage;
    float * amaze_red_storage;
    float * amaze_green_storage;
    float * amaze_blue_storage;
    uint32_t * amaze_gray;
    uint8_t * amaze_edge_direction;
    int * amaze_startchunk_y;
    int * amaze_endchunk_y;
    void * amaze_thread_id;
    void * amaze_arguments;
    size_t amaze_squeezed_capacity;
    size_t amaze_rawData_row_capacity;
    size_t amaze_red_row_capacity;
    size_t amaze_green_row_capacity;
    size_t amaze_blue_row_capacity;
    size_t amaze_row_capacity;
    size_t amaze_row_width;
    size_t amaze_rawData_plane_cell_capacity;
    size_t amaze_red_plane_cell_capacity;
    size_t amaze_green_plane_cell_capacity;
    size_t amaze_blue_plane_cell_capacity;
    size_t amaze_plane_cell_capacity;
    size_t amaze_gray_capacity;
    size_t amaze_edge_direction_capacity;
    size_t amaze_pixel_capacity;
    size_t amaze_startchunk_y_capacity;
    size_t amaze_endchunk_y_capacity;
    size_t amaze_thread_id_capacity;
    size_t amaze_arguments_capacity;
    size_t amaze_thread_capacity;

    uint16_t * alias_aux;
    size_t alias_aux_capacity;
} dualiso_full20bit_scratch_t;

typedef struct
{
    double total_ms;
    double pattern_ms;
    double noise_ms;
    double scratch_ms;
    double convert20_ms;
    double match_ms;
    double interp_ms;
    double interp_mean23_ms;
    double interp_amaze_ms;
    double interp_amaze_scratch_ms;
    double interp_amaze_clear_ms;
    double interp_amaze_squeeze_ms;
    double interp_amaze_rawdata_ms;
    double interp_amaze_demosaic_ms;
    double interp_amaze_demosaic_setup_ms;
    double interp_amaze_demosaic_create_ms;
    double interp_amaze_demosaic_join_ms;
    double interp_amaze_demosaic_worker_total_ms;
    double interp_amaze_demosaic_worker_max_ms;
    double interp_amaze_demosaic_worker_count;
    double interp_amaze_postprocess_ms;
    double interp_amaze_grayscale_ms;
    double interp_amaze_edge_init_ms;
    double interp_amaze_lut_ms;
    double interp_amaze_edge_direction_ms;
    double interp_amaze_edge_simd_batches;
    double interp_amaze_edge_allskip_batches;
    double interp_amaze_edge_mixed_batches;
    double interp_amaze_edge_fullsearch_batches;
    double interp_amaze_edge_fullsearch_pixels;
    double interp_amaze_edge_skip_pixels;
    double interp_amaze_edge_scalar_pixels;
    double interp_amaze_actual_interp_ms;
    double interp_border_ms;
    double fullres_ms;
    double mix_ms;
    double mix_curve_select_ms;
    double mix_curve_build_ms;
    double mix_curve_float_ms;
    double mix_ev_lut_ms;
    double mix_halfres_ms;
    double mix_halfres_avx2_bulk_ms;
    double mix_halfres_scalar_tail_ms;
    double mix_chroma_ms;
    double mix_chroma_copy_ms;
    double mix_chroma_fullres_ms;
    double mix_chroma_halfres_ms;
    double mix_chroma_horiz_probe_ms;
    double mix_chroma_vert_probe_ms;
    double mix_chroma_center_probe_ms;
    double mix_chroma_center_gather_probe_ms;
    double mix_chroma_center_arithmetic_probe_ms;
    double mix_chroma_center_store_probe_ms;
    double mix_chroma_center_store_r_probe_ms;
    double mix_chroma_center_store_b_probe_ms;
    double mix_chroma_center_store_r_lookup_probe_ms;
    double mix_chroma_center_store_b_lookup_probe_ms;
    double mix_chroma_center_average_probe_ms;
    double mix_chroma_center_non_average_probe_ms;
    double mix_chroma_center_non_average_choose_true_probe_ms;
    double mix_chroma_center_non_average_choose_false_probe_ms;
    double mix_chroma_center_write_both_count;
    double mix_chroma_center_write_r_only_count;
    double mix_chroma_center_write_b_only_count;
    double mix_chroma_center_write_none_count;
    double mix_chroma_center_use_average_count;
    double mix_chroma_center_choose_ev_lt_eh_count;
    double mix_chroma_halfres_center_probe_ms;
    double mix_chroma_halfres_center_gather_probe_ms;
    double mix_chroma_halfres_center_arithmetic_probe_ms;
    double mix_chroma_halfres_center_store_probe_ms;
    double mix_chroma_halfres_center_store_r_probe_ms;
    double mix_chroma_halfres_center_store_b_probe_ms;
    double mix_chroma_halfres_center_store_r_lookup_probe_ms;
    double mix_chroma_halfres_center_store_b_lookup_probe_ms;
    double mix_chroma_halfres_center_store_r_low_clamp_count;
    double mix_chroma_halfres_center_store_b_low_clamp_count;
    double mix_chroma_halfres_center_store_r_high_clamp_count;
    double mix_chroma_halfres_center_store_b_high_clamp_count;
    double mix_chroma_halfres_center_average_probe_ms;
    double mix_chroma_halfres_center_non_average_probe_ms;
    double mix_chroma_halfres_center_non_average_choose_true_probe_ms;
    double mix_chroma_halfres_center_non_average_choose_false_probe_ms;
    double mix_chroma_halfres_center_non_average_write_r_probe_ms;
    double mix_chroma_halfres_center_non_average_write_b_probe_ms;
    double mix_chroma_halfres_center_non_average_write_both_probe_ms;
    double mix_chroma_halfres_center_write_both_count;
    double mix_chroma_halfres_center_write_r_only_count;
    double mix_chroma_halfres_center_write_b_only_count;
    double mix_chroma_halfres_center_write_none_count;
    double mix_chroma_halfres_center_use_average_count;
    double mix_chroma_halfres_center_choose_ev_lt_eh_count;
    double mix_alias_map_ms;
    double mix_alias_map_setup_ms;
    double mix_alias_map_init_ms;
    double mix_alias_map_copy_ms;
    double mix_alias_map_filter_ms;
    double mix_alias_map_gaussian_ms;
    double mix_alias_map_grayscale_ms;
    double mix_overexposed_ms;
    double final_blend_setup_ms;
    double final_blend_row_kernel_ms;
    double final_blend_raw2ev_gather_probe_ms;
    double final_blend_fullres_curve_gather_probe_ms;
    double final_blend_ev2raw_store_probe_ms;
    double final_blend_arithmetic_probe_ms;
    double final_blend_overexposed_density;
    double final_blend_cap_clamp_pct;
    double final_blend_f_near_0_pct;
    double final_blend_f_near_1_pct;
    double final_blend_ms;
    double convert16_ms;
    double other_ms;
    double mix_curve_corr_ev;
    double mix_curve_overlap;
    int mix_chroma_probe_mode;
    int mix_chroma_probe_stage;
    int mix_halfres_probe_mode;
    int final_blend_probe_mode;
    int mix_curve_rebuilt;
    int mix_curve_global_hit;
    int path_kind;
    int input_width;
    int input_height;
    int playback_preview_scale_factor;
    int pattern_initial;
    int pattern_resolved;
    int pattern_source;
    int pattern_result;
    int phase_verify_enabled;
    int phase_probe_attempted;
    int phase_probe_succeeded;
    int phase_probe_decisive;
    int phase_probe_redetected;
    int phase_cached_pattern;
    int phase_implied_pattern;
    int interp_method;
    int use_alias_map;
    int use_fullres;
    int threads;
    int valid;
} dualiso_full20bit_timing_t;

typedef struct
{
    int valid;
    int width;
    int height;
    int black_level;
    int white_level;
    int white_darkened;
    int black_delta;
    double ev_correction;
    double dark_noise;
    int interp_method;
    int use_alias_map;
    int use_fullres;
    int chroma_smooth_method;
    int playback_preview_scale_factor;
    int is_bright[4];
    const int * raw2ev;
    const int * ev2raw;
    const double * mix_curve;
    const double * fullres_curve;
    const float * randn05;
    int apply_dither;
} dualiso_gpu_recon_state_t;

#ifdef __cplusplus
extern "C" {
#endif

extern DUALISO_THREAD_LOCAL dualiso_full20bit_timing_t g_dualiso_full20bit_timing;

int diso_get_preview(uint16_t * image_data, uint16_t width, uint16_t height, int32_t black, int32_t white, int * iso_pattern, int diso_check, dualiso_preview_scratch_t * scratch);
int diso_get_full20bit(struct raw_info raw_info, uint16_t * image_data, int dark_frame, int iso1, int iso2, int * iso_pattern, int * auto_correction, double * ev_correction, int * black_delta, int interp_method, int use_alias_map, int use_fullres, int chroma_smooth_method, int playback_preview_scale_factor, int threads, dualiso_full20bit_scratch_t * scratch);
int diso_prepare_gpu_recon_state(struct raw_info raw_info, uint16_t * image_data, int dark_frame, int iso1, int iso2, int * iso_pattern, int * auto_correction, double * ev_correction, int * black_delta, int interp_method, int use_alias_map, int use_fullres, int chroma_smooth_method, int playback_preview_scale_factor, int threads, dualiso_full20bit_scratch_t * scratch, dualiso_gpu_recon_state_t * state);
void free_dualiso_full20bit_scratch(dualiso_full20bit_scratch_t * scratch);
int dualiso_debug_get_last_gpu_recon_state(dualiso_gpu_recon_state_t * state);
void dualiso_debug_reset_last_gpu_recon_state(void);
void dualiso_debug_set_gpu_recon_state_capture_enabled(int enabled);
void dualiso_debug_reset_phase_verify_env_cache(void);

/* Test-only: counts how many times the HQ recon entered AMaZE (which == 0)
 * vs mean23 (which == 1) since the last reset. Used by pipeline tests to
 * verify the playback-mean23 override actually flipped the path without a
 * full pixel diff. Implemented as thread-local counters in dualiso.c. */
void dualiso_debug_note_hq_path(int which);
void dualiso_debug_reset_hq_path_counters(void);
unsigned long long dualiso_debug_hq_amaze_count(void);
unsigned long long dualiso_debug_hq_mean23_count(void);

/* Phase E5: thread-local counters for the alias_map and full-res blending
 * stages. Incremented when each stage is taken (use_alias_map != 0 /
 * use_fullres != 0) inside diso_get_full20bit. The reset peer is
 * dualiso_debug_reset_hq_path_counters above. Tests verify that the
 * scale-aware downgrade actually short-circuits these stages without
 * having to diff pixels against an alias_map-on reference. */
unsigned long long dualiso_debug_alias_map_taken_count(void);
unsigned long long dualiso_debug_fullres_blend_taken_count(void);
void dualiso_debug_reset_full20bit_timing(void);
void dualiso_debug_get_full20bit_timing(dualiso_full20bit_timing_t * timing);
int dualiso_mix_chroma_probe_mode(void);

/* LOOK-ASSIST-M16-CAST-3: the exposure match the HQ recon last ran on the calling thread (thread-local, measure-only).
 * rc is the match's return (<= 0 = failed); mode is the auto_correction it ran with (-1 nominal, -2 histogram, 0
 * explicit); ev is the applied correction in stops (positive); black_delta, black, white and white_darkened are 20-bit
 * units (64 = one 14-bit unit). With stop_after_match set, diso_get_full20bit returns 0 right after the match, so a
 * caller that only wants the numbers skips the reconstruction (the caller's failure path restores the frame). */
typedef struct
{
    int valid;
    int rc;
    int mode;
    double ev;
    int black_delta;
    int black;
    int white;
    int white_darkened;
} dualiso_match_probe_t;
void dualiso_match_probe_reset(int stop_after_match);
int dualiso_match_probe_get(dualiso_match_probe_t * probe);

/* LOOK-ASSIST-M16-CAST-4: measure-only overrides of the HQ recon's level and noise assumptions (thread-local). Set only
 * by llrawproc around an isolated analysis reconstruction and cleared right after it (NULL = today's recon):
 * white_bright (14-bit, > 0) replaces the assumed bright-field clip white/2 (clamped to white); dark_noise_scale (> 0)
 * multiplies the dark noise compute_noise found; dark_black_offset (when enabled, 14-bit codes per CFA channel R, G1,
 * G2, B) is subtracted from the dark-field rows before the match. */
typedef struct
{
    int white_bright;
    double dark_noise_scale;
    int dark_black_offset_enabled;
    int dark_black_offset[4];
} dualiso_analysis_override_t;
void dualiso_set_analysis_override(const dualiso_analysis_override_t * override_values);

/* LOOK-ASSIST-M16-CAST-4: the levels the HQ recon saw on the calling thread's last run (thread-local, measure-only):
 * per field (0 = dark rows, 1 = bright rows, from is_bright) and CFA channel (R, G1, G2, B) the 0.1 / 1 / 99.99
 * percentiles and the max of the 14-bit input (after restricted-range scaling), the stated levels, the bright clip the
 * recon assumed and used, and the noise model's inputs and outputs. With stop set, diso_get_full20bit returns 0 right
 * after recording them (the caller's failure path restores the frame). */
typedef struct
{
    int valid;
    int black;
    int white;
    int white_bright_default;
    int white_bright_used;
    int is_bright[4];
    int active_x1, active_y1, active_x2, active_y2;
    int has_noise_samples;
    double noise_std[4];
    double dark_noise;
    double bright_noise;
    int count[2][4];
    int p001[2][4];
    int p1[2][4];
    int p9999[2][4];
    int max[2][4];
} dualiso_levels_probe_t;
void dualiso_levels_probe_reset(int stop_after_levels);
int dualiso_levels_probe_get(dualiso_levels_probe_t * probe);

#ifdef __cplusplus
}
#endif

#endif
