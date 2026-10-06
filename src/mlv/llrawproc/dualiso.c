/*
 * Copyright (C) 2014 David Milligan
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

#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <limits.h>
#include "hist.h"
#include "dualiso.h"
#include "opt_med.h"
#include "wirth.h"
#include <pthread.h>
#if defined(__GNUC__) && !defined(__clang__) && (defined(__x86_64__) || defined(__i386__))
#include <immintrin.h>
#define DUALISO_AVX2_AVAILABLE 1
#endif
#include "../pipeline_stage_capture.h"
#include "../../debayer/debayer.h"
#include "../../debug/StageTiming.h"

#define EV_RESOLUTION 65536
#define DUALISO_RAW2EV_LUT_COUNT (1u << 20)
#define DUALISO_EV2RAW_LUT_COUNT (24u * EV_RESOLUTION)
#ifndef M_PI
#define M_PI 3.14159265358979323846 /* pi */
#endif

#define MIN(a,b) (((a)<(b))?(a):(b))
#define MAX(a,b) (((a)>(b))?(a):(b))
#define COERCE(x,lo,hi) MAX(MIN((x),(hi)),(lo))
#define ABS(a) ((a) > 0 ? (a) : -(a))

#define LOCK(x) static pthread_mutex_t x = PTHREAD_MUTEX_INITIALIZER; pthread_mutex_lock(&x);
#define UNLOCK(x) pthread_mutex_unlock(&(x));

/* Thread-local HQ recon path counters. Declared __thread so OMP-parallel
 * fixtures and concurrent test runners don't cross-contaminate each other.
 * Only the worker thread that drove diso_get_full20bit increments these;
 * tests reset and read them on the same thread that ran the render. */
#if defined(_MSC_VER)
#define DUALISO_THREAD_LOCAL __declspec(thread)
#else
#define DUALISO_THREAD_LOCAL __thread
#endif

static DUALISO_THREAD_LOCAL unsigned long long g_dualiso_hq_amaze_count = 0;
static DUALISO_THREAD_LOCAL unsigned long long g_dualiso_hq_mean23_count = 0;
static DUALISO_THREAD_LOCAL unsigned long long g_dualiso_alias_map_taken_count = 0;
static DUALISO_THREAD_LOCAL unsigned long long g_dualiso_fullres_blend_taken_count = 0;
DUALISO_THREAD_LOCAL dualiso_full20bit_timing_t g_dualiso_full20bit_timing = {0};
static DUALISO_THREAD_LOCAL dualiso_match_probe_t g_dualiso_match_probe = {0};
static DUALISO_THREAD_LOCAL int g_dualiso_match_probe_stop_after_match = 0;

void dualiso_match_probe_reset(int stop_after_match)
{
    memset(&g_dualiso_match_probe, 0, sizeof(g_dualiso_match_probe));
    g_dualiso_match_probe_stop_after_match = stop_after_match ? 1 : 0;
}

int dualiso_match_probe_get(dualiso_match_probe_t * probe)
{
    if (probe) *probe = g_dualiso_match_probe;
    return g_dualiso_match_probe.valid;
}
static DUALISO_THREAD_LOCAL dualiso_gpu_recon_state_t g_dualiso_last_gpu_recon_state = {0};
static DUALISO_THREAD_LOCAL int g_dualiso_gpu_recon_state_capture_enabled = 0;
static int g_dualiso_mix_chroma_probe_mode_cache = INT_MIN;
static int g_dualiso_mix_chroma_probe_stage_cache = 0;
static int g_dualiso_mix_halfres_probe_mode_cache = INT_MIN;
static int g_dualiso_final_blend_probe_mode_cache = INT_MIN;
static inline float * build_fullres_curve_float(dualiso_full20bit_scratch_t * scratch, int black);
static inline double * build_fullres_curve_float_as_double(dualiso_full20bit_scratch_t * scratch, int black);

enum
{
    DUALISO_GLOBAL_MIX_CURVE_CACHE_SLOTS = 4
};

typedef struct
{
    double * curve;
    size_t capacity;
    int valid;
    uint32_t black;
    uint32_t white;
    double corr_ev;
    double overlap;
    uint64_t last_used;
} dualiso_mix_curve_cache_entry_t;

static dualiso_mix_curve_cache_entry_t g_mix_curve_cache[DUALISO_GLOBAL_MIX_CURVE_CACHE_SLOTS];
static uint64_t g_mix_curve_cache_use_counter = 0;
static pthread_mutex_t g_mix_curve_cache_mutex = PTHREAD_MUTEX_INITIALIZER;

void dualiso_debug_note_hq_path(int which)
{
    if (which == 0) g_dualiso_hq_amaze_count++;
    else            g_dualiso_hq_mean23_count++;
}

void dualiso_debug_reset_hq_path_counters(void)
{
    g_dualiso_hq_amaze_count = 0;
    g_dualiso_hq_mean23_count = 0;
    g_dualiso_alias_map_taken_count = 0;
    g_dualiso_fullres_blend_taken_count = 0;
}

unsigned long long dualiso_debug_hq_amaze_count(void)
{
    return g_dualiso_hq_amaze_count;
}

unsigned long long dualiso_debug_hq_mean23_count(void)
{
    return g_dualiso_hq_mean23_count;
}

unsigned long long dualiso_debug_alias_map_taken_count(void)
{
    return g_dualiso_alias_map_taken_count;
}

unsigned long long dualiso_debug_fullres_blend_taken_count(void)
{
    return g_dualiso_fullres_blend_taken_count;
}

void dualiso_debug_reset_full20bit_timing(void)
{
    memset(&g_dualiso_full20bit_timing, 0, sizeof(g_dualiso_full20bit_timing));
    dualiso_debug_reset_last_gpu_recon_state();
    g_dualiso_full20bit_timing.interp_method = -1;
    g_dualiso_full20bit_timing.mix_chroma_probe_mode = -1;
    g_dualiso_full20bit_timing.mix_chroma_probe_stage = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_gather_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_arithmetic_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_r_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_b_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_store_r_lookup_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_store_b_lookup_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_average_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_non_average_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_non_average_choose_true_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_center_non_average_choose_false_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_r_lookup_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_b_lookup_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_r_low_clamp_count = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_b_low_clamp_count = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_r_high_clamp_count = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_store_b_high_clamp_count = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_average_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_choose_true_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_choose_false_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_write_r_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_write_b_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_center_non_average_write_both_probe_ms = 0;
    g_dualiso_full20bit_timing.mix_halfres_probe_mode = -1;
    g_dualiso_full20bit_timing.final_blend_probe_mode = -1;
    g_dualiso_full20bit_timing.path_kind = DUALISO_FULL20_PATH_NONE;
    g_dualiso_full20bit_timing.pattern_source = DUALISO_FULL20_PATTERN_SOURCE_NONE;
    g_dualiso_mix_chroma_probe_mode_cache = INT_MIN;
    g_dualiso_mix_chroma_probe_stage_cache = 0;
    g_dualiso_mix_halfres_probe_mode_cache = INT_MIN;
    g_dualiso_final_blend_probe_mode_cache = INT_MIN;
}

void dualiso_debug_get_full20bit_timing(dualiso_full20bit_timing_t * timing)
{
    if (!timing) return;
    *timing = g_dualiso_full20bit_timing;
}

void dualiso_debug_reset_last_gpu_recon_state(void)
{
    memset(&g_dualiso_last_gpu_recon_state, 0, sizeof(g_dualiso_last_gpu_recon_state));
}

void dualiso_debug_set_gpu_recon_state_capture_enabled(int enabled)
{
    g_dualiso_gpu_recon_state_capture_enabled = enabled != 0;
    if(!g_dualiso_gpu_recon_state_capture_enabled)
    {
        dualiso_debug_reset_last_gpu_recon_state();
    }
}

int dualiso_debug_get_last_gpu_recon_state(dualiso_gpu_recon_state_t * state)
{
    if (!state) return 0;
    *state = g_dualiso_last_gpu_recon_state;
    return state->valid != 0;
}

static void dualiso_debug_set_full20bit_path(int path_kind,
                                             struct raw_info raw_info,
                                             int interp_method,
                                             int use_alias_map,
                                             int use_fullres,
                                             int playback_preview_scale_factor,
                                             int threads)
{
    g_dualiso_full20bit_timing.path_kind = path_kind;
    g_dualiso_full20bit_timing.input_width = raw_info.width;
    g_dualiso_full20bit_timing.input_height = raw_info.height;
    g_dualiso_full20bit_timing.playback_preview_scale_factor =
        playback_preview_scale_factor;
    g_dualiso_full20bit_timing.interp_method = interp_method;
    g_dualiso_full20bit_timing.use_alias_map = use_alias_map != 0;
    g_dualiso_full20bit_timing.use_fullres = use_fullres != 0;
    g_dualiso_full20bit_timing.threads = threads;
    g_dualiso_full20bit_timing.valid = 1;
}

static int dualiso_debug_pattern_from_bright(const int * is_bright)
{
    static const int iso_patterns[4][4] = {
        {1, 1, 0, 0},
        {1, 0, 0, 1},
        {0, 0, 1, 1},
        {0, 1, 1, 0}
    };
    if (!is_bright) return 0;
    for (int i = 0; i < 4; i++)
    {
        if (memcmp(is_bright, iso_patterns[i], 4 * sizeof(int)) == 0)
            return i + 1;
    }
    return 0;
}

static void dualiso_debug_note_pattern_resolution(int source,
                                                  const int * iso_pattern,
                                                  const int * is_bright)
{
    g_dualiso_full20bit_timing.pattern_source = source;
    g_dualiso_full20bit_timing.pattern_resolved =
        iso_pattern ? *iso_pattern : 0;
    g_dualiso_full20bit_timing.pattern_result =
        dualiso_debug_pattern_from_bright(is_bright);
}

static int dualiso_env_truthy(const char * v);

int dualiso_mix_chroma_probe_mode(void)
{
    if (g_dualiso_mix_chroma_probe_mode_cache == INT_MIN)
    {
        const char * v = getenv("MLVAPP_DUALISO_MIX_CHROMA_PROBE");
        if (v && *v)
        {
            char * end = NULL;
            long parsed = strtol(v, &end, 10);
            if (end != v && *end == '\0' && parsed >= -1 && parsed <= 12)
            {
                g_dualiso_mix_chroma_probe_mode_cache = (int)parsed;
            }
            else if (!strcmp(v, "full") || !strcmp(v, "FULL"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 0;
            }
            else if (!strcmp(v, "horiz") || !strcmp(v, "HORIZ"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 1;
            }
            else if (!strcmp(v, "vert") || !strcmp(v, "VERT"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 2;
            }
            else if (!strcmp(v, "center") || !strcmp(v, "CENTER"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 3;
            }
            else if (!strcmp(v, "fullres") || !strcmp(v, "FULLRES"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 4;
            }
            else if (!strcmp(v, "average") || !strcmp(v, "AVERAGE"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 5;
            }
            else if (!strcmp(v, "nonaverage") || !strcmp(v, "NONAVERAGE") || !strcmp(v, "non-average") || !strcmp(v, "NON-AVERAGE"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 6;
            }
            else if (!strcmp(v, "nonaverage-true") || !strcmp(v, "NONAVERAGE-TRUE") || !strcmp(v, "non-average-true") || !strcmp(v, "NON-AVERAGE-TRUE"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 7;
            }
            else if (!strcmp(v, "nonaverage-false") || !strcmp(v, "NONAVERAGE-FALSE") || !strcmp(v, "non-average-false") || !strcmp(v, "NON-AVERAGE-FALSE"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 8;
            }
            else if (!strcmp(v, "nonaverage-write-r") || !strcmp(v, "NONAVERAGE-WRITE-R") || !strcmp(v, "non-average-write-r") || !strcmp(v, "NON-AVERAGE-WRITE-R"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 9;
            }
            else if (!strcmp(v, "nonaverage-write-b") || !strcmp(v, "NONAVERAGE-WRITE-B") || !strcmp(v, "non-average-write-b") || !strcmp(v, "NON-AVERAGE-WRITE-B"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 10;
            }
            else if (!strcmp(v, "nonaverage-store-r-clamp") || !strcmp(v, "NONAVERAGE-STORE-R-CLAMP") || !strcmp(v, "non-average-store-r-clamp") || !strcmp(v, "NON-AVERAGE-STORE-R-CLAMP"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 11;
            }
            else if (!strcmp(v, "nonaverage-store-b-clamp") || !strcmp(v, "NONAVERAGE-STORE-B-CLAMP") || !strcmp(v, "non-average-store-b-clamp") || !strcmp(v, "NON-AVERAGE-STORE-B-CLAMP"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 12;
            }
            else if (!strcmp(v, "nonaverage-write-both") || !strcmp(v, "NONAVERAGE-WRITE-BOTH") || !strcmp(v, "non-average-write-both") || !strcmp(v, "NON-AVERAGE-WRITE-BOTH"))
            {
                g_dualiso_mix_chroma_probe_mode_cache = 13;
            }
            else
            {
                g_dualiso_mix_chroma_probe_mode_cache = -1;
            }
        }
        else
        {
            g_dualiso_mix_chroma_probe_mode_cache = -1;
        }
    }
    return g_dualiso_mix_chroma_probe_mode_cache;
}

static int dualiso_mix_halfres_probe_mode(void)
{
    if (g_dualiso_mix_halfres_probe_mode_cache == INT_MIN)
    {
        g_dualiso_mix_halfres_probe_mode_cache =
            dualiso_env_truthy(getenv("MLVAPP_DUALISO_MIX_HALFRES_PROBE")) ? 1 : -1;
    }
    return g_dualiso_mix_halfres_probe_mode_cache;
}

static int dualiso_final_blend_probe_mode(void)
{
    if (g_dualiso_final_blend_probe_mode_cache == INT_MIN)
    {
        const char * v = getenv("MLVAPP_DUALISO_FULL20_FINAL_BLEND_PROBE");
        if (v && *v)
        {
            char * end = NULL;
            long parsed = strtol(v, &end, 10);
            if (end != v && *end == '\0' && parsed >= -1 && parsed <= 4)
            {
                g_dualiso_final_blend_probe_mode_cache = (int)parsed;
            }
            else if (!strcmp(v, "full") || !strcmp(v, "FULL"))
            {
                g_dualiso_final_blend_probe_mode_cache = 0;
            }
            else if (!strcmp(v, "raw2ev") || !strcmp(v, "RAW2EV"))
            {
                g_dualiso_final_blend_probe_mode_cache = 1;
            }
            else if (!strcmp(v, "fullres_curve") || !strcmp(v, "FULLRES_CURVE"))
            {
                g_dualiso_final_blend_probe_mode_cache = 2;
            }
            else if (!strcmp(v, "ev2raw_store") || !strcmp(v, "EV2RAW_STORE"))
            {
                g_dualiso_final_blend_probe_mode_cache = 3;
            }
            else if (!strcmp(v, "arithmetic") || !strcmp(v, "ARITHMETIC"))
            {
                g_dualiso_final_blend_probe_mode_cache = 4;
            }
            else
            {
                g_dualiso_final_blend_probe_mode_cache = -1;
            }
        }
        else
        {
            g_dualiso_final_blend_probe_mode_cache = -1;
        }
    }
    return g_dualiso_final_blend_probe_mode_cache;
}

static double dualiso_debug_elapsed_ms(double start)
{
    return (mlv_stage_timing_now() - start) * 1000.0;
}

static int dualiso_count_bits_u32(unsigned int mask)
{
    int count = 0;
    while (mask)
    {
        count += (int)(mask & 1u);
        mask >>= 1;
    }
    return count;
}

static void dualiso_debug_finish_full20bit_timing(double start)
{
    g_dualiso_full20bit_timing.total_ms = dualiso_debug_elapsed_ms(start);

    const double known_ms =
        g_dualiso_full20bit_timing.pattern_ms +
        g_dualiso_full20bit_timing.noise_ms +
        g_dualiso_full20bit_timing.scratch_ms +
        g_dualiso_full20bit_timing.convert20_ms +
        g_dualiso_full20bit_timing.match_ms +
        g_dualiso_full20bit_timing.interp_ms +
        g_dualiso_full20bit_timing.fullres_ms +
        g_dualiso_full20bit_timing.mix_ms +
        g_dualiso_full20bit_timing.final_blend_ms +
        g_dualiso_full20bit_timing.convert16_ms;
    g_dualiso_full20bit_timing.other_ms =
        MAX(0.0, g_dualiso_full20bit_timing.total_ms - known_ms);
}

/* ------------------------------------------------------------------------
 * Dual ISO preview rowscale: hot inner loop. Two implementations selected
 * once via pthread_once based on host CPU support and the
 * MLVAPP_DISABLE_AVX2 env-var kill switch. Mirrors the dispatch pattern at
 * src/processing/raw_processing.c:980-1066.
 *
 * The y-2 cross-row dependency (output[i - 2*width] is read after row y-2
 * has been written) means the OUTER y loop is sequential. Column-SIMD
 * within each row is safe.
 * ------------------------------------------------------------------------ */

typedef void (*dualiso_rowscale_fn_t)(int width, int height, int dark_row_start,
                                      const uint16_t * source_image,
                                      uint16_t * output_image,
                                      double a, double b,
                                      int32_t black, int32_t white,
                                      uint16_t shadow);

static void dualiso_rowscale_scalar(int width, int height, int dark_row_start,
                                    const uint16_t * source_image,
                                    uint16_t * output_image,
                                    double a, double b,
                                    int32_t black, int32_t white,
                                    uint16_t shadow)
{
    for(int y = 0; y < height; y++)
    {
        int row_start = y * width;
        if (((y - dark_row_start + 4) % 4) >= 2)
        {
            //bright row
            for(int i = row_start; i < row_start + width; i++)
            {
                if(source_image[i] >= white)
                {
                    output_image[i] = y > 2
                        ? (y < height - 2
                            ? (output_image[i-width*2] + source_image[i+width*2]) / 2
                            : output_image[i-width*2])
                        : source_image[i+width*2];
                }
                else
                {
                    output_image[i] = (uint16_t)(MIN(white,(source_image[i] - black) * a + black + b));
                }
            }
        }
        else
        {
            //dark row
            for(int i = row_start; i < row_start + width; i++)
            {
                if(source_image[i] < shadow)
                {
                    output_image[i] = (uint16_t)(y > 2
                        ? (y < height - 2
                            ? (output_image[i-width*2] + MIN(white,(source_image[i+width*2]  - black) * a + black + b)) / 2
                            : output_image[i-width*2])
                        : MIN(white,(source_image[i+width*2]  - black) * a + black + b));
                }
            }
        }
    }
}

#ifdef DUALISO_AVX2_AVAILABLE
__attribute__((target("avx2,fma")))
static void dualiso_rowscale_avx2(int width, int height, int dark_row_start,
                                  const uint16_t * source_image,
                                  uint16_t * output_image,
                                  double a, double b,
                                  int32_t black, int32_t white,
                                  uint16_t shadow)
{
    /* Hot kernel: 16 uint16/iter via two 8-float halves through FMA.
     * Edge rows use scalar fallbacks. The "edge" range is intentionally
     * wider than (y < 2 || y >= height - 2): the scalar reference treats
     * y == 2 (and y == height - 3) as a *boundary* case for saturated /
     * shadow lanes, where it skips the cross-row averaging that the
     * steady-state body performs. The SIMD body's saturated/shadow patch
     * unconditionally averages output[idx - 2w] with source[idx + 2w],
     * which is the y > 2 formula and disagrees with scalar at exactly the
     * y == 2 boundary. Treating y == 2 as an edge row pushes that boundary
     * back into the scalar fallback and yields byte-identity vs scalar.
     * Cost: 1 additional scalar row out of ~2200 (negligible). The
     * RowscaleAvx2ByteIdentityVsScalar parity test guards against a
     * regression on this boundary. */
    /* Constants in pd (double) so the FMA chain matches scalar bit-for-bit
     * on the values that previously drifted by 1 ULP in float32. The float
     * lo/hi vectors are kept for the integer<->float round trip and the
     * non-FMA tests; only the multiply/add chain runs in pd. */
    const __m256d ad     = _mm256_set1_pd(a);
    const __m256d blackd = _mm256_set1_pd((double)black);
    const __m256d zeroed = _mm256_setzero_pd();
    const __m256d whited = _mm256_set1_pd((double)white);
    /* Match scalar associativity exactly: `(((src - black) * a) + black) + b`,
     * NOT a 3-arg fmadd. Two separate add steps preserve scalar rounding. */
    const __m256d bd     = _mm256_set1_pd(b);
    const __m256i white_u16  = _mm256_set1_epi16((int16_t)white);
    const __m256i bias_xor   = _mm256_set1_epi16((int16_t)0x8000);
    const __m256i shadow_biased = _mm256_set1_epi16((int16_t)((shadow ^ 0x8000) & 0xFFFF));
    const __m256i zero_i = _mm256_setzero_si256();

    for(int y = 0; y < height; y++)
    {
        const int row_start = y * width;
        const int row_end   = row_start + width;
        const int is_bright = (((y - dark_row_start + 4) % 4) >= 2);
        /* Treat y in [0, 2] and [height-3, height-1] as edge so the SIMD
         * body only runs on rows where the y > 2 / y < height - 2 fast
         * paths in the scalar reference both apply. */
        const int edge_row  = (y < 3) || (y >= height - 3) || (width < 16);

        /* Edge rows or narrow widths: scalar full-row. Cheap because rare
         * (only 4 rows out of ~2200 typically). */
        if (edge_row)
        {
            if (is_bright)
            {
                for(int i = row_start; i < row_end; i++)
                {
                    if(source_image[i] >= white)
                    {
                        output_image[i] = y > 2
                            ? (y < height - 2
                                ? (output_image[i-width*2] + source_image[i+width*2]) / 2
                                : output_image[i-width*2])
                            : source_image[i+width*2];
                    }
                    else
                    {
                        output_image[i] = (uint16_t)(MIN(white,(source_image[i] - black) * a + black + b));
                    }
                }
            }
            else
            {
                for(int i = row_start; i < row_end; i++)
                {
                    if(source_image[i] < shadow)
                    {
                        output_image[i] = (uint16_t)(y > 2
                            ? (y < height - 2
                                ? (output_image[i-width*2] + MIN(white,(source_image[i+width*2]  - black) * a + black + b)) / 2
                                : output_image[i-width*2])
                            : MIN(white,(source_image[i+width*2]  - black) * a + black + b));
                    }
                }
            }
            continue;
        }

        const int simd_end = row_start + (width / 16) * 16;
        int i = row_start;

        if (is_bright)
        {
            for( ; i < simd_end; i += 16)
            {
                __m256i src_v = _mm256_loadu_si256((const __m256i*)&source_image[i]);

                /* Saturated mask: src >= white. max(src,white)==src iff src>=white. */
                __m256i sat_mask = _mm256_cmpeq_epi16(_mm256_max_epu16(src_v, white_u16), src_v);

                /* Convert to four pd-quartets (4 doubles per reg, 16 lanes
                 * total). The float intermediate path was 1 ULP off scalar
                 * for borderline values; full-double matches scalar
                 * byte-for-byte. */
                __m256i lo_i32 = _mm256_unpacklo_epi16(src_v, zero_i);
                __m256i hi_i32 = _mm256_unpackhi_epi16(src_v, zero_i);
                __m256d q0 = _mm256_cvtepi32_pd(_mm256_castsi256_si128(lo_i32));
                __m256d q1 = _mm256_cvtepi32_pd(_mm256_extracti128_si256(lo_i32, 1));
                __m256d q2 = _mm256_cvtepi32_pd(_mm256_castsi256_si128(hi_i32));
                __m256d q3 = _mm256_cvtepi32_pd(_mm256_extracti128_si256(hi_i32, 1));

                /* Match scalar: ((src - black) * a + black) + b. The two
                 * trailing adds are *not* fused so the rounding boundary
                 * matches the scalar reference's three rounding steps. */
                q0 = _mm256_add_pd(_mm256_add_pd(_mm256_mul_pd(_mm256_sub_pd(q0, blackd), ad), blackd), bd);
                q1 = _mm256_add_pd(_mm256_add_pd(_mm256_mul_pd(_mm256_sub_pd(q1, blackd), ad), blackd), bd);
                q2 = _mm256_add_pd(_mm256_add_pd(_mm256_mul_pd(_mm256_sub_pd(q2, blackd), ad), blackd), bd);
                q3 = _mm256_add_pd(_mm256_add_pd(_mm256_mul_pd(_mm256_sub_pd(q3, blackd), ad), blackd), bd);

                /* Clamp to [0, white]. The scalar uses MIN(white, .) but no
                 * lower bound — that's UB on negative values cast to uint16.
                 * The AVX2 path explicitly clamps to [0, white] which is the
                 * documented behaviour we want. */
                q0 = _mm256_min_pd(_mm256_max_pd(q0, zeroed), whited);
                q1 = _mm256_min_pd(_mm256_max_pd(q1, zeroed), whited);
                q2 = _mm256_min_pd(_mm256_max_pd(q2, zeroed), whited);
                q3 = _mm256_min_pd(_mm256_max_pd(q3, zeroed), whited);

                /* Truncate-to-int matches scalar's `(uint16_t)double_value`
                 * cast (truncation toward zero). */
                __m128i p0 = _mm256_cvttpd_epi32(q0);
                __m128i p1 = _mm256_cvttpd_epi32(q1);
                __m128i p2 = _mm256_cvttpd_epi32(q2);
                __m128i p3 = _mm256_cvttpd_epi32(q3);
                __m256i lo_p = _mm256_inserti128_si256(_mm256_castsi128_si256(p0), p1, 1);
                __m256i hi_p = _mm256_inserti128_si256(_mm256_castsi128_si256(p2), p3, 1);

                /* repack to uint16. With lo_p coming from unpacklo (src lanes
                 * [0..3, 8..11]) and hi_p from unpackhi (src lanes [4..7,
                 * 12..15]), _mm256_packus_epi32 lays out:
                 *   result[0..3]   = lo_p low 128  (= src[0..3])
                 *   result[4..7]   = hi_p low 128  (= src[4..7])
                 *   result[8..11]  = lo_p high 128 (= src[8..11])
                 *   result[12..15] = hi_p high 128 (= src[12..15])
                 * which is exactly src[0..15] in order, so NO permute is
                 * needed. A previous version of this kernel applied
                 * _mm256_permute4x64_epi64(scaled, 0xD8) here, which
                 * scrambled the already-correct layout and silently
                 * miscoloured the rowscaled bright/dark rows. The byte-
                 * identity test RowscaleAvx2ByteIdentityVsScalar guards
                 * against regression. See debayer.c:167-176 for the same
                 * pattern. */
                __m256i scaled = _mm256_packus_epi32(lo_p, hi_p);

                _mm256_storeu_si256((__m256i*)&output_image[i], scaled);

                /* Patch saturated lanes scalarly (rare path, <5% typical).
                 * The y == 2 boundary is in the edge_row fallback so the
                 * `y > 2` formula here is unconditionally correct for the
                 * SIMD-eligible rows. */
                int sat_mm = _mm256_movemask_epi8(sat_mask);
                if (sat_mm)
                {
                    for(int j = 0; j < 16; j++)
                    {
                        if ((sat_mm >> (j * 2)) & 0x3)
                        {
                            int idx = i + j;
                            output_image[idx] = (output_image[idx-width*2] + source_image[idx+width*2]) / 2;
                        }
                    }
                }
            }
            /* tail */
            for( ; i < row_end; i++)
            {
                if(source_image[i] >= white)
                {
                    output_image[i] = (output_image[i-width*2] + source_image[i+width*2]) / 2;
                }
                else
                {
                    output_image[i] = (uint16_t)(MIN(white,(source_image[i] - black) * a + black + b));
                }
            }
        }
        else /* dark row */
        {
            for( ; i < simd_end; i += 16)
            {
                __m256i src_v = _mm256_loadu_si256((const __m256i*)&source_image[i]);

                /* Shadow mask: src < shadow. _mm256_cmpgt_epi16 is signed; bias both ops by 0x8000. */
                __m256i src_biased = _mm256_xor_si256(src_v, bias_xor);
                __m256i shadow_mask = _mm256_cmpgt_epi16(shadow_biased, src_biased);

                int sm = _mm256_movemask_epi8(shadow_mask);
                if (sm)
                {
                    /* Scalar update only on shadow lanes. Most lanes leave
                     * output untouched (already initialised by the memcpy
                     * before this loop runs). */
                    for(int j = 0; j < 16; j++)
                    {
                        if ((sm >> (j * 2)) & 0x3)
                        {
                            int idx = i + j;
                            int scaled = (int)((source_image[idx+width*2] - black) * a + black + b);
                            if (scaled < 0)     scaled = 0;
                            if (scaled > white) scaled = white;
                            output_image[idx] = (uint16_t)((output_image[idx-width*2] + scaled) / 2);
                        }
                    }
                }
            }
            /* tail */
            for( ; i < row_end; i++)
            {
                if(source_image[i] < shadow)
                {
                    int scaled = (int)((source_image[i+width*2] - black) * a + black + b);
                    if (scaled < 0)     scaled = 0;
                    if (scaled > white) scaled = white;
                    output_image[i] = (uint16_t)((output_image[i-width*2] + scaled) / 2);
                }
            }
        }
    }
}
#endif /* DUALISO_AVX2_AVAILABLE */

static int dualiso_env_truthy(const char * v)
{
    if (!v || !*v) return 0;
    if (!strcmp(v, "1") || !strcmp(v, "true") || !strcmp(v, "TRUE")
     || !strcmp(v, "True") || !strcmp(v, "yes") || !strcmp(v, "on")) return 1;
    return 0;
}

static pthread_once_t g_dualiso_rowscale_dispatch_once = PTHREAD_ONCE_INIT;
static dualiso_rowscale_fn_t g_dualiso_rowscale_fn = NULL;
static int g_dualiso_rowscale_use_avx2 = 0;

static void dualiso_rowscale_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef DUALISO_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DUALISO"))) use_avx2 = 0;
    g_dualiso_rowscale_use_avx2 = use_avx2;
#ifdef DUALISO_AVX2_AVAILABLE
    g_dualiso_rowscale_fn = use_avx2 ? dualiso_rowscale_avx2 : dualiso_rowscale_scalar;
#else
    g_dualiso_rowscale_fn = dualiso_rowscale_scalar;
#endif
}

static void dualiso_rowscale(int width, int height, int dark_row_start,
                             const uint16_t * source_image,
                             uint16_t * output_image,
                             double a, double b,
                             int32_t black, int32_t white,
                             uint16_t shadow)
{
    pthread_once(&g_dualiso_rowscale_dispatch_once, dualiso_rowscale_dispatch_init);
    g_dualiso_rowscale_fn(width, height, dark_row_start,
                          source_image, output_image,
                          a, b, black, white, shadow);
}

int dualisoRowscaleAvx2Active(void)
{
    pthread_once(&g_dualiso_rowscale_dispatch_once, dualiso_rowscale_dispatch_init);
    return g_dualiso_rowscale_use_avx2;
}

/* Test-only hook: force re-evaluation of the dispatch from current env.
 * Mirrors dualisoHqReinitDispatchForTesting / debayerBasicU16ReinitDispatchForTesting.
 * Not in the public header; tests forward-declare it. */
int dualisoRowscaleReinitDispatchForTesting(void);
int dualisoRowscaleReinitDispatchForTesting(void)
{
    pthread_once(&g_dualiso_rowscale_dispatch_once, dualiso_rowscale_dispatch_init);
    dualiso_rowscale_dispatch_init();
    return g_dualiso_rowscale_use_avx2;
}

/* ====================================================================== *
 * AVX2 + FMA HQ Dual ISO recon kernels (Path B Phase B1 + B2).
 *
 * Five kernels are dispatched at runtime via pthread_once. Default ON when
 * the host advertises AVX2 + FMA. Set MLVAPP_DISABLE_AVX2_DUALISO_HQ=1
 * (or the global MLVAPP_DISABLE_AVX2=1) to force the scalar reference
 * paths for parity / regression debugging.
 *
 * The B0 prototype (.claude-state/profiling/20260424-phase-b0-final-blend/)
 * measured ~2.0× speedup p50 on the final_blend kernel alone, taking it
 * from ~20 ms to ~10 ms single-threaded on a 1808x2268 frame. With
 * Phase B1 wins on convert_to_20bit + fullres_reconstruction +
 * convert_20_to_16bit + Phase B2 mix_images, the full HQ recon should
 * land in the 10-14 ms range — well below the 16.7 ms / 60 fps budget.
 *
 * Drift bound: float32 FMA reordering may differ from scalar double-precision
 * by ±1 LSB on a small fraction (~0.2%) of pixels, far below the existing
 * raw_set_pixel_20to16_rand dither which already injects ~4 LSB random
 * noise per pixel. Pipeline tests assert ±1 LSB drift on a bounded fraction.
 * ====================================================================== */
#include "dualiso_avx2.inc"

static pthread_once_t g_dualiso_hq_dispatch_once = PTHREAD_ONCE_INIT;
static int g_dualiso_hq_use_avx2 = 0;

static void dualiso_hq_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef DUALISO_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DUALISO_HQ"))) use_avx2 = 0;
    g_dualiso_hq_use_avx2 = use_avx2;
}

int dualisoHqAvx2Active(void);
int dualisoHqAvx2Active(void)
{
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    return g_dualiso_hq_use_avx2;
}

/* Test-only hook: force re-evaluation of the dispatch from current env.
 * Mirrors debayerBasicU16ReinitDispatchForTesting. Not in the public
 * header; tests forward-declare it. */
int dualisoHqReinitDispatchForTesting(void);
int dualisoHqReinitDispatchForTesting(void)
{
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    dualiso_hq_dispatch_init();
    return g_dualiso_hq_use_avx2;
}

/* ====================================================================== *
 * Phase C4 — alias-map init err diff + 21-tap weighted Gaussian dispatch.
 *
 * Independent dispatch from g_dualiso_hq_use_avx2 so the parity test can
 * isolate the alias-map kernels. Kill switch:
 *   MLVAPP_DISABLE_AVX2_DUALISO_ALIAS_MAP=1   (also gated by the global
 *   MLVAPP_DISABLE_AVX2=1).
 *
 * Both new kernels are byte-identical to scalar:
 *   - init err diff: pure int32 arith (ABS/MIN/MAX/shifts), predicate masked
 *     to zero on writes; matches scalar `continue` against a pre-zeroed
 *     alias_map buffer.
 *   - 21-tap Gaussian: int32 multiply-and-shift mirrors `* W / 1024` per
 *     term-by-term, matching scalar truncation order; predicate preserves
 *     prior alias_map[x] via _mm256_blendv_epi8.
 * ====================================================================== */
static pthread_once_t g_dualiso_alias_dispatch_once = PTHREAD_ONCE_INIT;
static int g_dualiso_alias_use_avx2 = 0;

static void dualiso_alias_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef DUALISO_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DUALISO_ALIAS_MAP"))) use_avx2 = 0;
    g_dualiso_alias_use_avx2 = use_avx2;
}

int dualisoAliasMapAvx2Active(void);
int dualisoAliasMapAvx2Active(void)
{
    pthread_once(&g_dualiso_alias_dispatch_once, dualiso_alias_dispatch_init);
    return g_dualiso_alias_use_avx2;
}

/* Test-only hook: force re-evaluation of the dispatch from current env.
 * Mirrors dualisoHqReinitDispatchForTesting. Not in the public header;
 * tests forward-declare it. */
int dualisoAliasMapReinitDispatchForTesting(void);
int dualisoAliasMapReinitDispatchForTesting(void)
{
    pthread_once(&g_dualiso_alias_dispatch_once, dualiso_alias_dispatch_init);
    dualiso_alias_dispatch_init();
    return g_dualiso_alias_use_avx2;
}

/* ====================================================================== *
 * Phase E1 — AMaZE edge-direction estimator AVX2 dispatch.
 *
 * Independent dispatch from g_dualiso_hq_use_avx2 / g_dualiso_alias_use_avx2
 * so the parity test can isolate the E1 contribution. Kill switch:
 *   MLVAPP_DISABLE_AVX2_DUALISO_AMAZE=1   (also gated by the global
 *   MLVAPP_DISABLE_AVX2=1).
 *
 * The kernel is byte-identical to scalar: pure int32 arith
 * (ABS / sub / add / cmp) on raw2ev gather results. No FMA, no float
 * reordering, no division.
 *
 * The kernel itself lives in dualiso_amaze_avx2.inc (included earlier
 * in this TU; the include ordering keeps the helpers next to their
 * dispatch siblings). It depends on the file-scope `edge_directions[]`
 * table which is defined later in this TU at line ~2263 — to keep the
 * include order valid we wire the dispatch flags here but include the
 * .inc *after* edge_directions[] is declared (forward declaration of
 * the kernel below resolves the ordering).
 * ====================================================================== */
static pthread_once_t g_dualiso_amaze_dispatch_once = PTHREAD_ONCE_INIT;
static int g_dualiso_amaze_use_avx2 = 0;

static void dualiso_amaze_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef DUALISO_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (dualiso_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DUALISO_AMAZE"))) use_avx2 = 0;
    g_dualiso_amaze_use_avx2 = use_avx2;
}

int dualisoAmazeAvx2Active(void);
int dualisoAmazeAvx2Active(void)
{
    pthread_once(&g_dualiso_amaze_dispatch_once, dualiso_amaze_dispatch_init);
    return g_dualiso_amaze_use_avx2;
}

/* Test-only hook: force re-evaluation of the dispatch from current env.
 * Mirrors dualisoAliasMapReinitDispatchForTesting. Not in the public
 * header; tests forward-declare it. */
int dualisoAmazeReinitDispatchForTesting(void);
int dualisoAmazeReinitDispatchForTesting(void)
{
    pthread_once(&g_dualiso_amaze_dispatch_once, dualiso_amaze_dispatch_init);
    dualiso_amaze_dispatch_init();
    return g_dualiso_amaze_use_avx2;
}

static int preview_pattern_index(int iso_pattern)
{
    const int pattern = ABS(iso_pattern);
    return (pattern >= 1 && pattern <= 4) ? pattern : 0;
}

static int set_preview_histograms_from_pattern(int iso_pattern,
                                               struct histogram ** hist,
                                               uint16_t * dark_row_start,
                                               struct histogram ** hist_lo,
                                               struct histogram ** hist_hi)
{
    switch(preview_pattern_index(iso_pattern))
    {
    case 1:
        *dark_row_start = 2;
        *hist_lo = hist[2];
        *hist_hi = hist[0];
        return 1;
    case 2:
        *dark_row_start = 1;
        *hist_lo = hist[1];
        *hist_hi = hist[0];
        return 1;
    case 3:
        *dark_row_start = 0;
        *hist_lo = hist[0];
        *hist_hi = hist[2];
        return 1;
    case 4:
        *dark_row_start = 3;
        *hist_lo = hist[0];
        *hist_hi = hist[2];
        return 1;
    default:
        return 0;
    }
}

static int preview_pattern_from_dark_row_start(uint16_t dark_row_start)
{
    switch(dark_row_start)
    {
    case 0: return -3;
    case 1: return -2;
    case 2: return -1;
    case 3: return -4;
    default: return 0;
    }
}

static int ensure_preview_scratch_capacity(dualiso_preview_scratch_t * scratch, size_t data_size)
{
    if (!scratch)
    {
        return 0;
    }

    if (scratch->data_capacity >= data_size &&
        scratch->data_x &&
        scratch->data_y &&
        scratch->data_w)
    {
        return 1;
    }

    int * next_data_x = (int *)malloc(data_size * sizeof(scratch->data_x[0]));
    int * next_data_y = (int *)malloc(data_size * sizeof(scratch->data_y[0]));
    double * next_data_w = (double *)malloc(data_size * sizeof(scratch->data_w[0]));

    if (!next_data_x || !next_data_y || !next_data_w)
    {
        free(next_data_x);
        free(next_data_y);
        free(next_data_w);
        return 0;
    }

    free(scratch->data_x);
    free(scratch->data_y);
    free(scratch->data_w);
    scratch->data_x = next_data_x;
    scratch->data_y = next_data_y;
    scratch->data_w = next_data_w;
    scratch->data_capacity = data_size;
    return 1;
}

static int ensure_reusable_scratch_buffer(void ** buffer,
                                          size_t * capacity,
                                          size_t required_count,
                                          size_t element_size)
{
    if (!buffer || !capacity)
    {
        return 0;
    }

    if (*buffer && *capacity >= required_count)
    {
        return 1;
    }

    if (required_count == 0)
    {
        return 1;
    }

    if (element_size != 0 && required_count > (SIZE_MAX / element_size))
    {
        return 0;
    }

    void * next = malloc(required_count * element_size);
    if (!next)
    {
        return 0;
    }

    free(*buffer);
    *buffer = next;
    *capacity = required_count;
    return 1;
}

static double * ensure_double_scratch_buffer(double ** buffer, size_t * capacity, size_t required_count)
{
    return ensure_reusable_scratch_buffer((void **)buffer, capacity, required_count, sizeof(double))
        ? *buffer
        : NULL;
}

static float * ensure_float_scratch_buffer(float ** buffer, size_t * capacity, size_t required_count)
{
    return ensure_reusable_scratch_buffer((void **)buffer, capacity, required_count, sizeof(float))
        ? *buffer
        : NULL;
}

static uint16_t * ensure_preview_output_buffer(dualiso_preview_scratch_t * scratch, size_t required_count)
{
    return ensure_reusable_scratch_buffer((void **)&scratch->output_image,
                                          &scratch->output_capacity,
                                          required_count,
                                          sizeof(uint16_t))
        ? scratch->output_image
        : NULL;
}

static int * ensure_identify_histograms_scratch(dualiso_full20bit_scratch_t * scratch)
{
    const size_t histogram_count = 4;
    const size_t histogram_bins = 16384;
    const size_t required_count = histogram_count * histogram_bins;

    if (!scratch)
    {
        return NULL;
    }

    return ensure_reusable_scratch_buffer((void **)&scratch->identify_histograms,
                                          &scratch->identify_histogram_capacity,
                                          required_count,
                                          sizeof(int))
        ? scratch->identify_histograms
        : NULL;
}

static int ensure_histogram_match_scratch(dualiso_full20bit_scratch_t * scratch,
                                          size_t pixel_count,
                                          size_t sample_count,
                                          size_t highlight_count)
{
    if (!scratch
        || pixel_count == 0
        || sample_count == 0
        || highlight_count == 0
        || pixel_count > SIZE_MAX / sizeof(int)
        || sample_count > SIZE_MAX / sizeof(int)
        || highlight_count > SIZE_MAX / sizeof(int))
    {
        return 0;
    }

    if (scratch->histogram_match_dark
        && scratch->histogram_match_bright
        && scratch->histogram_match_tmp
        && scratch->histogram_match_hi_dark
        && scratch->histogram_match_hi_bright
        && scratch->histogram_match_pixel_capacity >= pixel_count
        && scratch->histogram_match_sample_capacity >= sample_count
        && scratch->histogram_match_highlight_capacity >= highlight_count)
    {
        return 1;
    }

    int * next_dark = (int *)malloc(pixel_count * sizeof(int));
    int * next_bright = (int *)malloc(pixel_count * sizeof(int));
    int * next_tmp = (int *)malloc(sample_count * sizeof(int));
    int * next_hi_dark = (int *)malloc(highlight_count * sizeof(int));
    int * next_hi_bright = (int *)malloc(highlight_count * sizeof(int));

    if (!next_dark || !next_bright || !next_tmp || !next_hi_dark || !next_hi_bright)
    {
        free(next_dark);
        free(next_bright);
        free(next_tmp);
        free(next_hi_dark);
        free(next_hi_bright);
        return 0;
    }

    free(scratch->histogram_match_dark);
    free(scratch->histogram_match_bright);
    free(scratch->histogram_match_tmp);
    free(scratch->histogram_match_hi_dark);
    free(scratch->histogram_match_hi_bright);

    scratch->histogram_match_dark = next_dark;
    scratch->histogram_match_bright = next_bright;
    scratch->histogram_match_tmp = next_tmp;
    scratch->histogram_match_hi_dark = next_hi_dark;
    scratch->histogram_match_hi_bright = next_hi_bright;
    scratch->histogram_match_pixel_capacity = pixel_count;
    scratch->histogram_match_sample_capacity = sample_count;
    scratch->histogram_match_highlight_capacity = highlight_count;
    return 1;
}

static void assign_amaze_plane_rows(float ** rows, float * storage, size_t row_count, size_t row_width)
{
    for (size_t i = 0; i < row_count; ++i)
    {
        rows[i] = storage + i * row_width;
    }
}

static int ensure_amaze_interpolation_scratch(dualiso_full20bit_scratch_t * scratch,
                                              size_t row_count,
                                              size_t row_width,
                                              size_t pixel_count,
                                              size_t thread_count)
{
    if (!scratch)
    {
        return 0;
    }

    if (thread_count == 0)
    {
        thread_count = 1;
    }

    const size_t plane_cell_count = row_count * row_width;

    if (!ensure_reusable_scratch_buffer((void **)&scratch->amaze_squeezed,
                                        &scratch->amaze_squeezed_capacity,
                                        row_count,
                                        sizeof(int))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_rawData_rows,
                                           &scratch->amaze_rawData_row_capacity,
                                           row_count,
                                           sizeof(float *))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_red_rows,
                                           &scratch->amaze_red_row_capacity,
                                           row_count,
                                           sizeof(float *))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_green_rows,
                                           &scratch->amaze_green_row_capacity,
                                           row_count,
                                           sizeof(float *))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_blue_rows,
                                           &scratch->amaze_blue_row_capacity,
                                           row_count,
                                           sizeof(float *))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_rawData_storage,
                                           &scratch->amaze_rawData_plane_cell_capacity,
                                           plane_cell_count,
                                           sizeof(float))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_red_storage,
                                           &scratch->amaze_red_plane_cell_capacity,
                                           plane_cell_count,
                                           sizeof(float))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_green_storage,
                                           &scratch->amaze_green_plane_cell_capacity,
                                           plane_cell_count,
                                           sizeof(float))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_blue_storage,
                                           &scratch->amaze_blue_plane_cell_capacity,
                                           plane_cell_count,
                                           sizeof(float)))
    {
        return 0;
    }

    scratch->amaze_row_capacity = MAX(scratch->amaze_squeezed_capacity,
                                      MAX(scratch->amaze_rawData_row_capacity,
                                      MAX(scratch->amaze_red_row_capacity,
                                      MAX(scratch->amaze_green_row_capacity,
                                          scratch->amaze_blue_row_capacity))));
    scratch->amaze_plane_cell_capacity = MAX(scratch->amaze_rawData_plane_cell_capacity,
                                             MAX(scratch->amaze_red_plane_cell_capacity,
                                             MAX(scratch->amaze_green_plane_cell_capacity,
                                                 scratch->amaze_blue_plane_cell_capacity)));
    scratch->amaze_row_width = MAX(scratch->amaze_row_width, row_width);
    assign_amaze_plane_rows(scratch->amaze_rawData_rows, scratch->amaze_rawData_storage, row_count, row_width);
    assign_amaze_plane_rows(scratch->amaze_red_rows, scratch->amaze_red_storage, row_count, row_width);
    assign_amaze_plane_rows(scratch->amaze_green_rows, scratch->amaze_green_storage, row_count, row_width);
    assign_amaze_plane_rows(scratch->amaze_blue_rows, scratch->amaze_blue_storage, row_count, row_width);

    if (!ensure_reusable_scratch_buffer((void **)&scratch->amaze_gray,
                                        &scratch->amaze_gray_capacity,
                                        pixel_count,
                                        sizeof(uint32_t))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_edge_direction,
                                           &scratch->amaze_edge_direction_capacity,
                                           pixel_count,
                                           sizeof(uint8_t)))
    {
        return 0;
    }

    scratch->amaze_pixel_capacity = MAX(scratch->amaze_gray_capacity,
                                        scratch->amaze_edge_direction_capacity);

    if (!ensure_reusable_scratch_buffer((void **)&scratch->amaze_startchunk_y,
                                        &scratch->amaze_startchunk_y_capacity,
                                        thread_count,
                                        sizeof(int))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_endchunk_y,
                                           &scratch->amaze_endchunk_y_capacity,
                                           thread_count,
                                           sizeof(int))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_thread_id,
                                           &scratch->amaze_thread_id_capacity,
                                           thread_count,
                                           sizeof(pthread_t))
        || !ensure_reusable_scratch_buffer((void **)&scratch->amaze_arguments,
                                           &scratch->amaze_arguments_capacity,
                                           thread_count,
                                           sizeof(amazeinfo_t)))
    {
        return 0;
    }

    scratch->amaze_thread_capacity = MAX(scratch->amaze_startchunk_y_capacity,
                                         MAX(scratch->amaze_endchunk_y_capacity,
                                         MAX(scratch->amaze_thread_id_capacity,
                                             scratch->amaze_arguments_capacity)));

    return 1;
}

static uint16_t * ensure_alias_aux_scratch(dualiso_full20bit_scratch_t * scratch, size_t pixel_count)
{
    if (!scratch)
    {
        return NULL;
    }

    return ensure_reusable_scratch_buffer((void **)&scratch->alias_aux,
                                          &scratch->alias_aux_capacity,
                                          pixel_count,
                                          sizeof(uint16_t))
        ? scratch->alias_aux
        : NULL;
}

void free_dualiso_full20bit_scratch(dualiso_full20bit_scratch_t * scratch)
{
    if (!scratch)
    {
        return;
    }

    free(scratch->raw_buffer_32);
    free(scratch->dark);
    free(scratch->bright);
    free(scratch->fullres);
    free(scratch->halfres);
    free(scratch->fullres_smooth);
    free(scratch->halfres_smooth);
    free(scratch->overexposed);
    free(scratch->alias_map);
    free(scratch->over_aux);
    free(scratch->ev_raw2ev);
    free(scratch->ev_raw2ev_float);
    free(scratch->ev2raw_0);
    for (int i = 0; i < DUALISO_MIX_CURVE_CACHE_SLOTS; i++)
    {
        free(scratch->mix_curve[i]);
        free(scratch->mix_curve_float[i]);
    }
    free(scratch->fullres_curve);
    free(scratch->fullres_curve_float);
    free(scratch->fullres_curve_float_as_double);
    free(scratch->histogram_match_dark);
    free(scratch->histogram_match_bright);
    free(scratch->histogram_match_tmp);
    free(scratch->histogram_match_hi_dark);
    free(scratch->histogram_match_hi_bright);
    free(scratch->identify_histograms);
    free(scratch->amaze_squeezed);
    free(scratch->amaze_rawData_rows);
    free(scratch->amaze_red_rows);
    free(scratch->amaze_green_rows);
    free(scratch->amaze_blue_rows);
    free(scratch->amaze_rawData_storage);
    free(scratch->amaze_red_storage);
    free(scratch->amaze_green_storage);
    free(scratch->amaze_blue_storage);
    free(scratch->amaze_gray);
    free(scratch->amaze_edge_direction);
    free(scratch->amaze_startchunk_y);
    free(scratch->amaze_endchunk_y);
    free(scratch->amaze_thread_id);
    free(scratch->amaze_arguments);
    free(scratch->alias_aux);
    memset(scratch, 0, sizeof(*scratch));
}

static int ensure_full20bit_pixel_capacity(dualiso_full20bit_scratch_t * scratch, size_t pixel_count)
{
    if (!scratch
        || pixel_count == 0
        || pixel_count > SIZE_MAX / sizeof(uint32_t)
        || pixel_count > SIZE_MAX / sizeof(uint16_t))
    {
        return 0;
    }

    if (scratch->pixel_capacity >= pixel_count
        && scratch->raw_buffer_32
        && scratch->dark
        && scratch->bright
        && scratch->fullres
        && scratch->halfres
        && scratch->fullres_smooth
        && scratch->halfres_smooth
        && scratch->overexposed
        && scratch->alias_map
        && scratch->over_aux)
    {
        return 1;
    }

    uint32_t * next_raw = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_dark = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_bright = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_fullres = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_halfres = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_fullres_smooth = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint32_t * next_halfres_smooth = (uint32_t *)malloc(pixel_count * sizeof(uint32_t));
    uint16_t * next_overexposed = (uint16_t *)malloc(pixel_count * sizeof(uint16_t));
    uint16_t * next_alias_map = (uint16_t *)malloc(pixel_count * sizeof(uint16_t));
    uint16_t * next_over_aux = (uint16_t *)malloc(pixel_count * sizeof(uint16_t));

    if (!next_raw
        || !next_dark
        || !next_bright
        || !next_fullres
        || !next_halfres
        || !next_fullres_smooth
        || !next_halfres_smooth
        || !next_overexposed
        || !next_alias_map
        || !next_over_aux)
    {
        free(next_raw);
        free(next_dark);
        free(next_bright);
        free(next_fullres);
        free(next_halfres);
        free(next_fullres_smooth);
        free(next_halfres_smooth);
        free(next_overexposed);
        free(next_alias_map);
        free(next_over_aux);
        return 0;
    }

    free(scratch->raw_buffer_32);
    free(scratch->dark);
    free(scratch->bright);
    free(scratch->fullres);
    free(scratch->halfres);
    free(scratch->fullres_smooth);
    free(scratch->halfres_smooth);
    free(scratch->overexposed);
    free(scratch->alias_map);
    free(scratch->over_aux);

    scratch->raw_buffer_32 = next_raw;
    scratch->dark = next_dark;
    scratch->bright = next_bright;
    scratch->fullres = next_fullres;
    scratch->halfres = next_halfres;
    scratch->fullres_smooth = next_fullres_smooth;
    scratch->halfres_smooth = next_halfres_smooth;
    scratch->overexposed = next_overexposed;
    scratch->alias_map = next_alias_map;
    scratch->over_aux = next_over_aux;
    scratch->pixel_capacity = pixel_count;
    return 1;
}

//this is just meant to be fast
int diso_get_preview(uint16_t * image_data, uint16_t width, uint16_t height, int32_t black, int32_t white, int * iso_pattern, int diso_check, dualiso_preview_scratch_t * scratch)
{
    struct histogram * hist[4];
    struct histogram * hist_hi = NULL;
    struct histogram * hist_lo = NULL;
    const double histogram_start = mlv_stage_timing_now();

    if( scratch )
    {
        scratch->last_histogram_ms = 0.0;
        scratch->last_regression_ms = 0.0;
        scratch->last_rowscale_ms = 0.0;
    }
    
    for(int i = 0; i < 4; i++)
    {
        if(scratch)
        {
            if(scratch->histograms[i] == NULL)
                scratch->histograms[i] = hist_create((uint16_t)white);
            else if(!hist_reset(scratch->histograms[i], (uint16_t)white))
                return 0;
            hist[i] = scratch->histograms[i];
        }
        else
        {
            hist[i] = hist_create((uint16_t)white);
        }
        if(hist[i] == NULL) return 0;
    }
    
    for(uint16_t y = 4; y < height - 4; y += 5)
    {
        hist_add(hist[y % 4], &(image_data[y * width + (y + 1) % 2]), width - (y + 1) % 2, 3);
    }
    
    uint16_t dark_row_start = UINT16_MAX;
    const int cached_pattern = iso_pattern ? *iso_pattern : 0;
    if( !set_preview_histograms_from_pattern(cached_pattern, hist, &dark_row_start, &hist_lo, &hist_hi) )
    {
        uint16_t median[4];
        for(int i = 0; i < 4; i++)
        {
            median[i] = hist_median(hist[i]);
        }

        if((median[2] - black) > ((median[0] - black) * 2) &&
           (median[2] - black) > ((median[1] - black) * 2) &&
           (median[3] - black) > ((median[0] - black) * 2) &&
           (median[3] - black) > ((median[1] - black) * 2))
        {
            dark_row_start = 0;
            hist_lo = hist[0];
            hist_hi = hist[2];
        }
        else if((median[0] - black) > ((median[1] - black) * 2) &&
                (median[0] - black) > ((median[2] - black) * 2) &&
                (median[3] - black) > ((median[1] - black) * 2) &&
                (median[3] - black) > ((median[2] - black) * 2))
        {
            dark_row_start = 1;
            hist_lo = hist[1];
            hist_hi = hist[0];
        }
        else if((median[0] - black) > ((median[2] - black) * 2) &&
                (median[0] - black) > ((median[3] - black) * 2) &&
                (median[1] - black) > ((median[2] - black) * 2) &&
                (median[1] - black) > ((median[3] - black) * 2))
        {
            dark_row_start = 2;
            hist_lo = hist[2];
            hist_hi = hist[0];
        }
        else if((median[1] - black) > ((median[0] - black) * 2) &&
                (median[1] - black) > ((median[3] - black) * 2) &&
                (median[2] - black) > ((median[0] - black) * 2) &&
                (median[2] - black) > ((median[3] - black) * 2))
        {
            dark_row_start = 3;
            hist_lo = hist[0];
            hist_hi = hist[2];
        }
        else
        {
#ifndef STDOUT_SILENT
            err_printf("\nCould not detect dual ISO interlaced lines\n");
#endif

            for(int i = 0; i < 4; i++)
            {
                if(!scratch) hist_destroy(hist[i]);
            }
            if( scratch )
            {
                scratch->last_histogram_ms = (mlv_stage_timing_now() - histogram_start) * 1000.0;
            }
            return 0;
        }

        if( iso_pattern )
        {
            *iso_pattern = preview_pattern_from_dark_row_start(dark_row_start);
        }
    }

    if(diso_check)
    {
#ifndef STDOUT_SILENT
        err_printf("\nDetected dual ISO interlaced lines\n");
#endif

        for(int i = 0; i < 4; i++)
        {
            if(!scratch) hist_destroy(hist[i]);
        }
        if( scratch )
        {
            scratch->last_histogram_ms = (mlv_stage_timing_now() - histogram_start) * 1000.0;
        }
        return 1;
    }

    if( scratch )
    {
        scratch->last_histogram_ms = (mlv_stage_timing_now() - histogram_start) * 1000.0;
    }

    /* compare the two histograms and plot the curve between the two exposures (dark as a function of bright) */
    const int min_pix = 100;                                /* extract a data point every N image pixels */
    int data_size = (width * height / min_pix + 1);                  /* max number of data points */
    int* data_x = NULL;
    int* data_y = NULL;
    double* data_w = NULL;
    const int using_scratch = scratch && ensure_preview_scratch_capacity(scratch, data_size);
    const double regression_start = mlv_stage_timing_now();

    if( using_scratch )
    {
        data_x = scratch->data_x;
        data_y = scratch->data_y;
        data_w = scratch->data_w;
    }
    else
    {
        data_x = (int *)malloc(data_size * sizeof(data_x[0]));
        data_y = (int *)malloc(data_size * sizeof(data_y[0]));
        data_w = (double *)malloc(data_size * sizeof(data_w[0]));
    }

    if (!data_x || !data_y || !data_w)
    {
        for(int i = 0; i < 4; i++)
        {
            if(!scratch) hist_destroy(hist[i]);
        }

        if (!using_scratch)
        {
            free(data_x);
            free(data_y);
            free(data_w);
        }
        if( scratch )
        {
            scratch->last_regression_ms = (mlv_stage_timing_now() - regression_start) * 1000.0;
        }
        return 0;
    }

    int data_num = 0;
    
    int acc_lo = 0;
    int acc_hi = 0;
    int raw_lo = 0;
    int raw_hi = 0;
    int prev_acc_hi = 0;
    
    int hist_total = hist[0]->count;

    /* Iterate over histogram BINS (sized white+1 in hist_create), not the
     * pixel count. The previous loop bound `< hist_total` caused
     * out-of-bounds reads at hist_hi->data[raw_hi] whenever the source
     * frame contained more pixels than the white level (i.e., almost
     * always at 1080p+). The percentile thresholds at the data_w cutoff
     * below still use hist_total correctly. */
    for (raw_hi = 0; raw_hi <= hist_hi->white; raw_hi++)
    {
        acc_hi += hist_get_bin(hist_hi, raw_hi);

        while (acc_lo < acc_hi)
        {
            acc_lo += hist_get_bin(hist_lo, raw_lo);
            raw_lo++;
        }

        if (raw_lo >= white)
            break;

        if (acc_hi - prev_acc_hi > min_pix)
        {
            if (acc_hi > hist_total * 1 / 100 && acc_hi < hist_total * 99.99 / 100)    /* throw away outliers */
            {
                data_x[data_num] = raw_hi - black;
                data_y[data_num] = raw_lo - black;
                data_w[data_num] = (MAX(0, raw_hi - black + 100));    /* points from higher brightness are cleaner */
                data_num++;
                prev_acc_hi = acc_hi;
            }
        }
    }
    
    /**
     * plain least squares
     * y = ax + b
     * a = (mean(xy) - mean(x)mean(y)) / (mean(x^2) - mean(x)^2)
     * b = mean(y) - a mean(x)
     */
    
    double mx = 0, my = 0, mxy = 0, mx2 = 0;
    double weight = 0;
    for (int i = 0; i < data_num; i++)
    {
        mx += data_x[i] * data_w[i];
        my += data_y[i] * data_w[i];
        mxy += (double)data_x[i] * data_y[i] * data_w[i];
        mx2 += (double)data_x[i] * data_x[i] * data_w[i];
        weight += data_w[i];
    }
    mx /= weight;
    my /= weight;
    mxy /= weight;
    mx2 /= weight;

    /* Guard against zero-variance bright-row sample. When all bright
     * samples are at the same x-value (e.g. clipped highlights or a flat
     * patch), the regression denominator (mx2 - mx*mx) collapses to zero
     * and `a` becomes NaN/inf. NaN then propagates into 1/(a*a) at the
     * shadow computation and the per-pixel scale at lines below, casting
     * to undefined uint16 values that render as the magenta/cyan cast
     * users observed during playback (2026-04-24). Fall back to the
     * median-ratio scale (my/mx) and zero offset when variance is below
     * a small epsilon — that's the right "no slope detected" default
     * for the dual-ISO pair-up. */
    const double DUALISO_VARIANCE_EPSILON = 1e-9;
    const double denom = mx2 - mx * mx;
    double a, b;
    if (fabs(denom) < DUALISO_VARIANCE_EPSILON)
    {
        a = (fabs(mx) > DUALISO_VARIANCE_EPSILON) ? (my / mx) : 1.0;
        b = 0.0;
    }
    else
    {
        a = (mxy - mx * my) / denom;
        b = my - a * mx;
    }
    
    if (!using_scratch)
    {
        free(data_w);
        free(data_y);
        free(data_x);
    }

    for(int i = 0; i < 4; i++)
    {
        if(!scratch) hist_destroy(hist[i]);
    }
    if( scratch )
    {
        scratch->last_regression_ms = (mlv_stage_timing_now() - regression_start) * 1000.0;
    }
    
    //TODO: what's a better way to pick a value for this?
    uint16_t shadow = (uint16_t)(black + 1 / (a * a) + b);
    const double rowscale_start = mlv_stage_timing_now();
    const size_t pixel_count = (size_t)width * (size_t)height;
    uint16_t * output_image = scratch ? ensure_preview_output_buffer(scratch, pixel_count) : NULL;
    const uint16_t * source_image = image_data;

    if( output_image )
    {
        memcpy(output_image, source_image, pixel_count * sizeof(uint16_t));
    }
    else
    {
        output_image = image_data;
    }

    /* Hot inner loop. Dispatched once via pthread_once to either an AVX2
     * vectorised implementation or a portable scalar fallback. The y-2
     * cross-row dependency forces sequential outer iteration; SIMD
     * happens within each row. See the dispatch helpers near the top of
     * this file. */
    dualiso_rowscale(width, height, dark_row_start,
                     source_image, output_image,
                     a, b,
                     black, white, shadow);

    if( output_image != image_data )
    {
        memcpy(image_data, output_image, pixel_count * sizeof(uint16_t));
    }

    if( scratch )
    {
        scratch->last_rowscale_ms = (mlv_stage_timing_now() - rowscale_start) * 1000.0;
    }

    return 1;
}


//from cr2hdr 20bit version
//this is not thread safe (yet)

#define BRIGHT_ROW (is_bright[y % 4])
#define COUNT(x) ((int)(sizeof(x)/sizeof((x)[0])))

#define raw_get_pixel(x,y) (image_data[(x) + (y) * raw_info.width])
#define raw_get_pixel16(x,y) (image_data[(x) + (y) * raw_info.width])
#define raw_get_pixel_14to20(x,y) ((((uint32_t)image_data[(x) + (y) * raw_info.width]) << 6) & 0xFFFFF)
#define raw_get_pixel32(x,y) (raw_buffer_32[(x) + (y) * raw_info.width])
#define raw_set_pixel32(x,y,value) raw_buffer_32[(x) + (y)*raw_info.width] = value
#define raw_get_pixel_20to16(x,y) ((raw_get_pixel32(x,y) >> 4) & 0xFFFF)
#define raw_set_pixel_20to16_rand(x,y,value) image_data[(x) + (y) * raw_info.width] = COERCE((int)((value) / 16.0 + randn05_cache[((x) + (y) * raw_info.width) & 1023] + 0.5), 0, 0xFFFF)
#define raw_set_pixel20(x,y,value) raw_buffer_32[(x) + (y) * raw_info.width] = COERCE((value), 0, 0xFFFFF)

static const double fullres_thr = 0.8;

/* trial and error - too high = aliasing, too low = noisy */
static const int ALIAS_MAP_MAX = 15000;

static inline uint16_t alias_map_fifth_largest_37_at(const uint16_t * alias_map, int w, int x, int y)
{
    uint16_t best0 = 0;
    uint16_t best1 = 0;
    uint16_t best2 = 0;
    uint16_t best3 = 0;
    uint16_t best4 = 0;

    const uint16_t * ym6 = alias_map + (size_t)(y - 6) * (size_t)w;
    const uint16_t * ym4 = alias_map + (size_t)(y - 4) * (size_t)w;
    const uint16_t * ym2 = alias_map + (size_t)(y - 2) * (size_t)w;
    const uint16_t * y0  = alias_map + (size_t)y       * (size_t)w;
    const uint16_t * yp2 = alias_map + (size_t)(y + 2) * (size_t)w;
    const uint16_t * yp4 = alias_map + (size_t)(y + 4) * (size_t)w;
    const uint16_t * yp6 = alias_map + (size_t)(y + 6) * (size_t)w;

#define ALIAS_MAP_TOP5_VALUE(value) \
    do { \
        const uint16_t alias_value = (uint16_t)(value); \
        if (alias_value > best4) \
        { \
            if (alias_value > best3) \
            { \
                best4 = best3; \
                if (alias_value > best2) \
                { \
                    best3 = best2; \
                    if (alias_value > best1) \
                    { \
                        best2 = best1; \
                        if (alias_value > best0) \
                        { \
                            best1 = best0; \
                            best0 = alias_value; \
                        } \
                        else \
                        { \
                            best1 = alias_value; \
                        } \
                    } \
                    else \
                    { \
                        best2 = alias_value; \
                    } \
                } \
                else \
                { \
                    best3 = alias_value; \
                } \
            } \
            else \
            { \
                best4 = alias_value; \
            } \
        } \
    } while (0)
#define ALIAS_MAP_TOP5(row, offset) ALIAS_MAP_TOP5_VALUE((row)[x + (offset)])
                                                              ALIAS_MAP_TOP5(ym6, -2); ALIAS_MAP_TOP5(ym6,  0); ALIAS_MAP_TOP5(ym6,  2);
                                  ALIAS_MAP_TOP5(ym4, -4); ALIAS_MAP_TOP5(ym4, -2); ALIAS_MAP_TOP5(ym4,  0); ALIAS_MAP_TOP5(ym4,  2); ALIAS_MAP_TOP5(ym4,  4);
    ALIAS_MAP_TOP5(ym2, -6); ALIAS_MAP_TOP5(ym2, -4); ALIAS_MAP_TOP5(ym2, -2); ALIAS_MAP_TOP5(ym2,  0); ALIAS_MAP_TOP5(ym2,  2); ALIAS_MAP_TOP5(ym2,  4); ALIAS_MAP_TOP5(ym2,  6);
    ALIAS_MAP_TOP5(y0,  -6); ALIAS_MAP_TOP5(y0,  -4); ALIAS_MAP_TOP5(y0,  -2); ALIAS_MAP_TOP5(y0,   0); ALIAS_MAP_TOP5(y0,   2); ALIAS_MAP_TOP5(y0,   4); ALIAS_MAP_TOP5(y0,   6);
    ALIAS_MAP_TOP5(yp2, -6); ALIAS_MAP_TOP5(yp2, -4); ALIAS_MAP_TOP5(yp2, -2); ALIAS_MAP_TOP5(yp2,  0); ALIAS_MAP_TOP5(yp2,  2); ALIAS_MAP_TOP5(yp2,  4); ALIAS_MAP_TOP5(yp2,  6);
                                  ALIAS_MAP_TOP5(yp4, -4); ALIAS_MAP_TOP5(yp4, -2); ALIAS_MAP_TOP5(yp4,  0); ALIAS_MAP_TOP5(yp4,  2); ALIAS_MAP_TOP5(yp4,  4);
                                                              ALIAS_MAP_TOP5(yp6, -2); ALIAS_MAP_TOP5(yp6,  0); ALIAS_MAP_TOP5(yp6,  2);
#undef ALIAS_MAP_TOP5
#undef ALIAS_MAP_TOP5_VALUE

    return best4;
}

static void white_detect(struct raw_info raw_info, uint16_t * image_data, int* white_dark, int* white_bright, int * is_bright)
{
    /* sometimes the white level is much lower than 15000; this would cause pink highlights */
    /* workaround: consider the white level as a little under the maximum pixel value from the raw file */
    /* caveat: bright and dark exposure may have different white levels, so we'll take the minimum value */
    /* side effect: if the image is not overexposed, it may get brightened a little; shouldn't hurt */
    
    int whites[2]         = {  0,    0};
    int discard_pixels[2] = { 10,   50}; /* discard the brightest N pixels */
    int safety_margins[2] = {100, 1500}; /* use a higher safety margin for the higher ISO */
    /* note: with the high-ISO WL underestimated by 1500, you would lose around 0.15 EV of non-aliased detail */
    
    int* pixels[2];
    int max_pix = raw_info.width * raw_info.height / 2 / 9;
    pixels[0] = malloc(max_pix * sizeof(pixels[0][0]));
    pixels[1] = malloc(max_pix * sizeof(pixels[0][0]));
    memset(pixels[0], 0, sizeof(max_pix * sizeof(pixels[0][0])));
    memset(pixels[1], 0, sizeof(max_pix * sizeof(pixels[0][0])));
    int counts[2] = {0, 0};
    
    /* collect all the pixels and find the k-th max, thus ignoring hot pixels */
    /* change the sign in order to use kth_smallest_int */
    //#pragma omp parallel for collapse(2)
    for (int y = raw_info.active_area.y1; y < raw_info.active_area.y2; y += 3)
    {
        for (int x = raw_info.active_area.x1; x < raw_info.active_area.x2; x += 3)
        {
            int pix = raw_get_pixel16(x, y);

#define BIN_IDX is_bright[y%4]
            counts[BIN_IDX] = MIN(counts[BIN_IDX], max_pix-1);
            pixels[BIN_IDX][counts[BIN_IDX]] = -pix;
            counts[BIN_IDX]++;
#undef BIN_IDX
        }
    }
    
    whites[0] = -kth_smallest_int(pixels[0], counts[0], discard_pixels[0]) - safety_margins[0];
    whites[1] = -kth_smallest_int(pixels[1], counts[1], discard_pixels[1]) - safety_margins[1];
    
    //~ printf("%8d %8d\n", whites[0], whites[1]);
    //~ printf("%8d %8d\n", counts[0], counts[1]);
    
    /* we assume 14-bit input data; out-of-range white levels may cause crash */
    *white_dark = COERCE(whites[0], 10000, 16383);
    *white_bright = COERCE(whites[1], 5000, 16383);
#ifndef STDOUT_SILENT
    printf("White levels    : %d %d\n", *white_dark, *white_bright);
#endif
    free(pixels[0]);
    free(pixels[1]);
}

static void compute_black_noise(struct raw_info raw_info, uint16_t * image_data, int x1, int x2, int y1, int y2, int dx, int dy, double* out_mean, double* out_stdev)
{
    long long black = 0;
    int num = 0;
    /* compute average level */
    #pragma omp parallel for collapse(2) reduction(+:black,num)
    for (int y = y1; y < y2; y += dy)
    {
        for (int x = x1; x < x2; x += dx)
        {
            black += raw_get_pixel(x, y);
            num++;
        }
    }
    
    if (num < 2)
    {
        *out_mean = raw_info.black_level;
        *out_stdev = 8.0; /* default to 11 stops of DR */
        return;
    }

    double mean = (double) black / num;
    
    /* compute standard deviation */
    double stdev = 0;
    #pragma omp parallel for collapse(2) reduction(+:stdev)
    for (int y = y1; y < y2; y += dy)
    {
        for (int x = x1; x < x2; x += dx)
        {
            double dif = raw_get_pixel(x, y) - mean;
            stdev += dif * dif;
        }
    }
    stdev /= (num-1);
    stdev = sqrt(stdev);
    
    *out_mean = mean;
    *out_stdev = stdev;
}

static inline int mean2_noerr(int a, int b, int white)
{
    if (a >= white || b >= white)
        return white;

    return (a + b) / 2;
}

static inline int mean3_noerr(int a, int b, int c, int white)
{
    int m = (a + b + c) / 3;

    if (a >= white || b >= white || c >= white)
        return MAX(m, white);

    return m;
}

/* http://www.developpez.net/forums/d544518/c-cpp/c/equivalent-randn-matlab-c/#post3241904 */

#define TWOPI (6.2831853071795864769252867665590057683943387987502) /* 2 * pi */

/*
 RAND is a macro which returns a pseudo-random numbers from a uniform
 distribution on the interval [0 1]
 */
#define RAND (rand())/((double) RAND_MAX)

/*
 RANDN is a macro which returns a pseudo-random numbers from a normal
 distribution with mean zero and standard deviation one. This macro uses Box
 Muller's algorithm
 */
#define RANDN (sqrt(-2.0*log(RAND))*cos(TWOPI*RAND))

/* anti-posterization noise */
/* before rounding, it's a good idea to add a Gaussian noise of stdev=0.5 */
static float randn05_cache[1024];

void fast_randn_init()
{
    int i;
    for (i = 0; i < 1024; i++)
    {
        randn05_cache[i] = RANDN / 2;
    }
}

float fast_randn05()
{
    static int k = 0;
    return randn05_cache[(k++) & 1023];
}

static int identify_rggb_or_gbrg(struct raw_info raw_info,
                                 uint16_t * image_data,
                                 dualiso_full20bit_scratch_t * scratch)
{
    int w = raw_info.width;
    int h = raw_info.height;
    int * histogram_storage = ensure_identify_histograms_scratch(scratch);

    if (!histogram_storage)
    {
        return 0;
    }

    /* build 4 little histograms: one for red, one for blue and two for green */
    /* we don't know yet which channels are which, but that's what we are trying to find out */
    /* the ones with the smallest difference are likely the green channels */
    int* hist[4];
    for (int i = 0; i < 4; i++)
    {
        hist[i] = histogram_storage + (i * 16384);
    }
    memset(histogram_storage, 0, 4 * 16384 * sizeof(int));
    
    int y0 = (raw_info.active_area.y1 + 3) & ~3;
    
    /* to simplify things, analyze an identical number of bright and dark lines */
    #pragma omp parallel for collapse(2)
    for (int y = y0; y < h/4*4; y++)
    {
        for (int x = 0; x < w; x++)
            hist[(y%2)*2 + (x%2)][raw_get_pixel16(x,y) & 16383]++;
    }
    
    /* compute cdf */
    for (int k = 0; k < 4; k++)
    {
        int acc = 0;
        for (int i = 0; i < 16384; i++)
        {
            acc += hist[k][i];
            hist[k][i] = acc;
        }
    }
    
    /* compare cdf's */
    /* for rggb, greens are at y%2 != x%2, that is, 1 and 2 */
    /* for gbrg, greens are at y%2 == x%2, that is, 0 and 3 */
    double diffs_rggb = 0;
    double diffs_gbrg = 0;
    //#pragma omp parallel for
    for (int i = 0; i < 16384; i++)
    {
        diffs_rggb += ABS(hist[1][i] - hist[2][i]);
        diffs_gbrg += ABS(hist[0][i] - hist[3][i]);
    }
    
    /* which one is most likely? */
    return diffs_rggb < diffs_gbrg;
}

static int identify_bright_and_dark_fields(struct raw_info raw_info,
                                           uint16_t * image_data,
                                           int rggb,
                                           int * is_bright,
                                           dualiso_full20bit_scratch_t * scratch)
{
    (void)rggb;
    /* first we need to know which lines are dark and which are bright */
    /* the pattern is not always the same, so we need to autodetect it */
    
    /* it may look like this */                       /* or like this */
    /*
     ab cd ef gh  ab cd ef gh               ab cd ef gh  ab cd ef gh
     
     0  RG RG RG RG  RG RG RG RG            0  rg rg rg rg  rg rg rg rg
     1  gb gb gb gb  gb gb gb gb            1  gb gb gb gb  gb gb gb gb
     2  rg rg rg rg  rg rg rg rg            2  RG RG RG RG  RG RG RG RG
     3  GB GB GB GB  GB GB GB GB            3  GB GB GB GB  GB GB GB GB
     4  RG RG RG RG  RG RG RG RG            4  rg rg rg rg  rg rg rg rg
     5  gb gb gb gb  gb gb gb gb            5  gb gb gb gb  gb gb gb gb
     6  rg rg rg rg  rg rg rg rg            6  RG RG RG RG  RG RG RG RG
     7  GB GB GB GB  GB GB GB GB            7  GB GB GB GB  GB GB GB GB
     8  RG RG RG RG  RG RG RG RG            8  rg rg rg rg  rg rg rg rg
     */
    
    /* white level is not yet known, just use a rough guess */
    int white = 10000;
    int black = raw_info.black_level;
    
    int w = raw_info.width;
    int h = raw_info.height;
    
    int * histogram_storage = ensure_identify_histograms_scratch(scratch);
    if (!histogram_storage)
    {
        return 0;
    }

    /* build 4 little histograms */
    int* hist[4];
    for (int i = 0; i < 4; i++)
    {
        hist[i] = histogram_storage + (i * 16384);
    }
    memset(histogram_storage, 0, 4 * 16384 * sizeof(int));
    
    int y0 = (raw_info.active_area.y1 + 3) & ~3;
    
    /* to simplify things, analyze an identical number of bright and dark lines */
    for (int y = y0; y < h/4*4; y++)
    {
        for (int x = 0; x < w; x++)
        {
            if ((x%2) != (y%2))
            {
                /* only check the green pixels */
                hist[y%4][raw_get_pixel16(x,y) & 16383]++;
            }
        }
    }
    
    int hist_total = 0;
    for (int i = 0; i < 16384; i++)
        hist_total += hist[0][i];
    
    /* choose the highest percentile that is not overexposed */
    /* but not higher than 99.8, to keep a tiny bit of robustness (specular highlights may play dirty tricks) */
    int acc[4] = {0};
    int raw[4] = {0};
    int off[4] = {0};
    int ref;
    int ref_max = hist_total * 0.998;
    int ref_off = hist_total * 0.05;
    for (ref = 0; ref < ref_max; ref++)
    {
        for (int i = 0; i < 4; i++)
        {
            while (acc[i] < ref)
            {
                acc[i] += hist[i][raw[i]];
                raw[i]++;
            }
        }
        
        if (ref < ref_off)
        {
            if (MAX(MAX(raw[0], raw[1]), MAX(raw[2], raw[3])) < black + (white-black) / 4)
            {
                /* try to remove the black offset by estimating it from relatively dark pixels */
                off[0] = raw[0];
                off[1] = raw[1];
                off[2] = raw[2];
                off[3] = raw[3];
            }
        }
        
        if (raw[0] >= white) break;
        if (raw[1] >= white) break;
        if (raw[2] >= white) break;
        if (raw[3] >= white) break;
    }
    
    /* remove black offsets */
    raw[0] -= off[0];
    raw[1] -= off[1];
    raw[2] -= off[2];
    raw[3] -= off[3];
    
    /* very crude way to compute median */
    int sorted_bright[4];
    memcpy(sorted_bright, raw, sizeof(sorted_bright));
    {
        for (int i = 0; i < 4; i++)
        {
            for (int j = i+1; j < 4; j++)
            {
                if (sorted_bright[i] > sorted_bright[j])
                {
                    double aux = sorted_bright[i];
                    sorted_bright[i] = sorted_bright[j];
                    sorted_bright[j] = aux;
                }
            }
        }
    }
    double median_bright = (sorted_bright[1] + sorted_bright[2]) / 2;
    
    for (int i = 0; i < 4; i++)
        is_bright[i] = raw[i] > median_bright;
#ifndef STDOUT_SILENT
    printf("ISO pattern     : %c%c%c%c %s\n", is_bright[0] ? 'B' : 'd', is_bright[1] ? 'B' : 'd', is_bright[2] ? 'B' : 'd', is_bright[3] ? 'B' : 'd', rggb ? "RGGB" : "GBRG");
#endif
    if (is_bright[0] + is_bright[1] + is_bright[2] + is_bright[3] != 2)
    {
#ifndef STDOUT_SILENT
        printf("Bright/dark detection error\n");
#endif
        return 0;
    }
    
    if (is_bright[0] == is_bright[2] || is_bright[1] == is_bright[3])
    {
#ifndef STDOUT_SILENT
        printf("Interlacing method not supported\n");
#endif
        return 0;
    }
    return 1;
}

static int dualiso_checked_histogram_geometry(int width,
                                              int height,
                                              int active_y1,
                                              size_t * pixel_count,
                                              size_t * sample_capacity,
                                              size_t * highlight_capacity,
                                              int * first_sample_y)
{
    if (!pixel_count || !sample_capacity || !highlight_capacity || !first_sample_y
        || width <= 0 || height < 5 || active_y1 < 0 || active_y1 > height - 5)
    {
        return 0;
    }

    const size_t checked_width = (size_t)width;
    const size_t checked_height = (size_t)height;
    if (checked_width > SIZE_MAX / checked_height)
    {
        return 0;
    }

    const size_t checked_pixels = checked_width * checked_height;
    if (checked_pixels > (size_t)INT_MAX || checked_pixels > SIZE_MAX / sizeof(int))
    {
        return 0;
    }

    /* Preserve the historical floor((w + 2) * (h + 2) / 9) capacity exactly,
     * but perform the additions and product in the allocation's size domain. */
    const size_t padded_width = checked_width + 2u;
    const size_t padded_height = checked_height + 2u;
    if (padded_width > SIZE_MAX / padded_height)
    {
        return 0;
    }

    const size_t checked_samples = (padded_width * padded_height) / 9u;
    if (checked_samples == 0
        || checked_samples > (size_t)INT_MAX
        || checked_samples > SIZE_MAX / sizeof(int))
    {
        return 0;
    }

    const size_t checked_highlights = MAX(checked_samples / 50u, (size_t)1u);
    if (checked_highlights > (size_t)INT_MAX
        || checked_highlights > SIZE_MAX / sizeof(int))
    {
        return 0;
    }

    *pixel_count = checked_pixels;
    *sample_capacity = checked_samples;
    *highlight_capacity = checked_highlights;
    *first_sample_y = active_y1 + 2;
    return 1;
}

static int dualiso_checked_full20_frame_geometry(struct raw_info raw_info,
                                                 size_t * pixel_count)
{
    if (!pixel_count
        || raw_info.width < 5
        || raw_info.height < 6
        || raw_info.black_level < 0
        || raw_info.white_level <= raw_info.black_level
        || raw_info.black_level > 16383
        || raw_info.white_level > 16383
        /* A 16383-code signal maps to exactly +14 EV.  ev2raw's positive
         * domain ends at +14 EV exclusive, so that metadata endpoint would
         * become a one-past lookup in mean23 interpolation. */
        || raw_info.white_level - raw_info.black_level >= 16383
        || raw_info.black_level > INT_MAX / 64
        || raw_info.white_level > INT_MAX / 64
        || raw_info.active_area.x1 < 0
        || raw_info.active_area.y1 < 0
        || raw_info.active_area.x2 < raw_info.active_area.x1
        || raw_info.active_area.y2 < raw_info.active_area.y1
        || raw_info.active_area.x2 > raw_info.width
        || raw_info.active_area.y2 > raw_info.height)
    {
        return 0;
    }

    const size_t checked_width = (size_t)raw_info.width;
    const size_t checked_height = (size_t)raw_info.height;
    if (checked_width > SIZE_MAX / checked_height)
    {
        return 0;
    }

    const size_t checked_pixels = checked_width * checked_height;
    if (checked_pixels == 0
        || checked_pixels > (size_t)INT_MAX
        || checked_pixels > SIZE_MAX / sizeof(uint32_t)
        || checked_pixels > SIZE_MAX / sizeof(uint16_t))
    {
        return 0;
    }

    *pixel_count = checked_pixels;
    return 1;
}

/* Production-linked test seam for hostile geometry without allocating a
 * synthetic multi-gigabyte frame. Not part of the public llrawproc API. */
int dualisoHistogramGeometryForTesting(int width,
                                       int height,
                                       int active_y1,
                                       size_t * pixel_count,
                                       size_t * sample_capacity,
                                       size_t * highlight_capacity,
                                       int * first_sample_y);
int dualisoHistogramGeometryForTesting(int width,
                                       int height,
                                       int active_y1,
                                       size_t * pixel_count,
                                       size_t * sample_capacity,
                                       size_t * highlight_capacity,
                                       int * first_sample_y)
{
    return dualiso_checked_histogram_geometry(width,
                                              height,
                                              active_y1,
                                              pixel_count,
                                              sample_capacity,
                                              highlight_capacity,
                                              first_sample_y);
}

static int match_by_histogram(struct raw_info raw_info,
                              uint32_t * raw_buffer_32,
                              double * ev_correction,
                              int * black_delta,
                              int * white_darkened,
                              int * is_bright,
                              dualiso_full20bit_scratch_t * scratch)
{
    if (!raw_buffer_32 || !ev_correction || !black_delta || !white_darkened || !is_bright || !scratch)
    {
        return 0;
    }

    /* guess ISO - find the factor and the offset for matching the bright and dark images */
    int black20 = raw_info.black_level;
    int white20 = MIN(raw_info.white_level, *white_darkened);
    if (black20 < 0 || white20 <= black20)
    {
        return 0;
    }
    int black = black20/16;
    int white = white20/16;
    if (white <= black)
    {
        return 0;
    }
    int clip0 = white - black;
    int clip  = clip0 * 0.95;    /* there may be nonlinear response in very bright areas */

    int w = raw_info.width;
    int h = raw_info.height;
    int y0 = 0;
    size_t pixel_count = 0;
    size_t sample_capacity = 0;
    size_t highlight_capacity = 0;
    if (!dualiso_checked_histogram_geometry(w,
                                            h,
                                            raw_info.active_area.y1,
                                            &pixel_count,
                                            &sample_capacity,
                                            &highlight_capacity,
                                            &y0))
    {
        return 0;
    }

    /* quick interpolation for matching */
    if (!ensure_histogram_match_scratch(scratch,
                                        pixel_count,
                                        sample_capacity,
                                        highlight_capacity))
    {
        return 0;
    }

    int * dark = scratch->histogram_match_dark;
    int * bright = scratch->histogram_match_bright;

    //#pragma omp parallel for
    for (int y = y0; y < h-2; y += 3)
    {
        int* native = BRIGHT_ROW ? bright : dark;
        int* interp = BRIGHT_ROW ? dark : bright;

        for (int x = 0; x < w; x += 3)
        {
            int pa = raw_get_pixel_20to16(x, y-2) - black;
            int pb = raw_get_pixel_20to16(x, y+2) - black;
            int pn = raw_get_pixel_20to16(x, y) - black;
            int pi = (pa + pb + 1) / 2;
            if (pa >= clip || pb >= clip) pi = clip0;               /* pixel too bright? discard */
            if (pi >= clip) pn = clip0;                             /* interpolated pixel not good? discard the other one too */
            const size_t pixel_index = (size_t)y * (size_t)w + (size_t)x;
            interp[pixel_index] = pi;
            native[pixel_index] = pn;
        }
    }

    /*
     * Robust line fit (match unclipped data):
     * - use (median_bright, median_dark) as origin
     * - select highlights between 98 and 99.9th percentile to find the slope (ISO)
     * - choose the slope that explains the largest number of highlight points (inspired from RANSAC)
     *
     * Rationale:
     * - exposure matching is important to be correct in bright_highlights (which are combined with dark_midtones)
     * - low percentiles are likely affected by noise (this process is essentially a histogram matching)
     * - as ad-hoc as it looks, it's the only method that passed all the test samples so far.
     */
    int * tmp = scratch->histogram_match_tmp;

    /* median_bright */
    size_t n = 0;
    for (int y = y0; y < h-2; y += 3)
    {
        for (int x = 0; x < w; x += 3)
        {
            const size_t pixel_index = (size_t)y * (size_t)w + (size_t)x;
            int b = bright[pixel_index];
            if (b >= clip) continue;
            if (n >= sample_capacity) return 0;
            tmp[n++] = b;
        }
    }
    if (n == 0) return 0;
    const int bright_sample_count = (int)n;
    int bmed = median_int_wirth(tmp, bright_sample_count);

    /* also compute the range for bright pixels (used to find the slope) */
    const int bright_lo_index = (int)(((uint64_t)n * 98u) / 100u);
    const int bright_hi_index = (int)(((uint64_t)n * 999u) / 1000u);
    int b_lo = kth_smallest_int(tmp, bright_sample_count, bright_lo_index);
    int b_hi = kth_smallest_int(tmp, bright_sample_count, bright_hi_index);
    if (b_hi <= b_lo) return 0;

    /* median_dark */
    n = 0;
    for (int y = y0; y < h-2; y += 3)
    {
        for (int x = 0; x < w; x += 3)
        {
            const size_t pixel_index = (size_t)y * (size_t)w + (size_t)x;
            int d = dark[pixel_index];
            int b = bright[pixel_index];
            if (b >= clip) continue;
            if (n >= sample_capacity) return 0;
            tmp[n++] = d;
        }
    }
    if (n == 0) return 0;
    int dmed = median_int_wirth(tmp, (int)n);

    /* select highlights used to find the slope (ISO) */
    /* (98th percentile => up to 2% highlights) */
    size_t hi_n = 0;
    int* hi_dark = scratch->histogram_match_hi_dark;
    int* hi_bright = scratch->histogram_match_hi_bright;

    for (int y = y0; y < h-2 && hi_n < highlight_capacity; y += 3)
    {
        for (int x = 0; x < w; x += 3)
        {
            const size_t pixel_index = (size_t)y * (size_t)w + (size_t)x;
            int d = dark[pixel_index];
            int b = bright[pixel_index];
            if (b >= b_hi) continue;
            if (b <= b_lo) continue;
            if (hi_n >= highlight_capacity) break;
            hi_dark[hi_n] = d;
            hi_bright[hi_n] = b;
            hi_n++;
        }
    }
    if (hi_n == 0) return 0;

    //~ printf("Selected %zu highlight points (max %zu)\n", hi_n, highlight_capacity);

    double a = 0;
    double b = 0;

    int best_score = 0;
    for (double ev = 0; ev < 6; ev += 0.01)
    {
        double test_a = pow(2, -ev);
        double test_b = dmed - bmed * test_a;

        int score = 0;
        for (size_t i = 0; i < hi_n; i++)
        {
            int d = hi_dark[i];
            int b = hi_bright[i];
            int e = d - (b*test_a + test_b);
            if (ABS(e) < 50) score++;
        }
        if (score > best_score)
        {
            best_score = score;
            a = test_a;
            b = test_b;
            //~ printf("%f: %d\n", a, score);
        }
    }

    if (best_score <= 0 || !(a > 0.0) || !isfinite(a) || !isfinite(b))
    {
        return 0;
    }

    const double matched_ev = log2(1.0 / a);
    const double matched_black_delta = b * 16.0;
    if (!isfinite(matched_ev)
        || !isfinite(matched_black_delta)
        || matched_black_delta < (double)INT_MIN
        || matched_black_delta > (double)INT_MAX)
    {
        return 0;
    }

    *ev_correction = matched_ev;
    *black_delta = (int)matched_black_delta;
    return 1;
}

static int compute_match_exposure_scalars(struct raw_info raw_info,
                                          uint32_t * raw_buffer_32,
                                          int dark_frame,
                                          int iso1,
                                          int iso2,
                                          int * auto_correction,
                                          double * ev_correction,
                                          int * black_delta,
                                          int * white_darkened,
                                          int * is_bright,
                                          dualiso_full20bit_scratch_t * scratch,
                                          int allow_histogram,
                                          double * applied_ev_correction,
                                          int * applied_black_delta)
{
    int black = raw_info.black_level;
    int white = MIN(raw_info.white_level, *white_darkened);

    double _ev_correction = 0.0;
    int _black_delta = 0;

    if (*auto_correction == -1)
    {
        int low_iso = MIN(iso1, iso2);
        int high_iso = MAX(iso1, iso2);

        if (low_iso <= 0 || high_iso <= 0)
        {
            return 0;
        }

        /* Preserve the historical integer-ratio exposure contract. The
         * positive-ISO guard above closes division by zero without changing
         * valid rendered output for non-integral ISO ratios. */
        _ev_correction = log2(high_iso / low_iso);

        // ISO 6400 having the same brightness as ISO 3200 on 650D. Not sure about other cameras.
        // Anyway, ISO 6400 and higher is considerred useless.
        if (high_iso >= 6400 && _ev_correction > 0)
        {
            high_iso /= 2;
            _ev_correction -= 1;
        }

        if (!dark_frame)
        {
            _black_delta = ((high_iso / 100) * 64) - ((low_iso / 100) * 64);
        }
    }
    else if (*auto_correction == -2)
    {
        if (!allow_histogram || !raw_buffer_32)
        {
            return -1;
        }
        if (!match_by_histogram(raw_info,
                                raw_buffer_32,
                                &_ev_correction,
                                &_black_delta,
                                white_darkened,
                                is_bright,
                                scratch))
        {
            return 0;
        }
    }

    if (*ev_correction != 1)
    {
        _ev_correction = -*ev_correction;
    }

    if (*black_delta != -1)
    {
        if (*black_delta < INT_MIN / 64 || *black_delta > INT_MAX / 64)
        {
            return 0;
        }
        _black_delta = *black_delta * 64;
    }

    if (!isfinite(_ev_correction))
    {
        return 0;
    }
    _ev_correction = COERCE(_ev_correction, 0, 6.0);
    _black_delta = COERCE(_black_delta, 0, 100 * 64);

    if (_ev_correction < 0.5)
    {
#ifndef STDOUT_SILENT
        printf("Doesn't look like interlaced ISO.\n");
#endif
        return 0;
    }

    const double factor = pow(2, -_ev_correction);
    const double matched_signal_span =
        (double)white - (double)black + (double)_black_delta;
    const double matched_white_darkened =
        (matched_signal_span * factor) + (double)black;
    if (!isfinite(factor)
        || !isfinite(matched_signal_span)
        || !isfinite(matched_white_darkened)
        || matched_white_darkened < 0.0
        || matched_white_darkened >= (double)(1u << 20)
        || matched_white_darkened - (double)black
            >= (double)(16383u * 64u))
    {
        return 0;
    }

    if (applied_ev_correction)
    {
        *applied_ev_correction = _ev_correction;
    }
    if (applied_black_delta)
    {
        *applied_black_delta = _black_delta;
    }
    *ev_correction = -_ev_correction;
    *black_delta = _black_delta / 64;
    *white_darkened = (int)matched_white_darkened;

    //printf("DISO: %d, %.2f, %d\n", *auto_correction, *ev_correction, *black_delta);

    return 1;
}

static int match_exposures(struct raw_info raw_info,
                           uint32_t * raw_buffer_32,
                           int dark_frame,
                           int iso1,
                           int iso2,
                           int * auto_correction,
                           double * ev_correction,
                           int * black_delta,
                           int * white_darkened,
                           int * is_bright,
                           dualiso_full20bit_scratch_t * scratch)
{
    int black = raw_info.black_level;
    int w = raw_info.width;
    int h = raw_info.height;
    double applied_ev_correction = 0.0;
    int applied_black_delta = 0;
    const int probe_mode = auto_correction ? *auto_correction : 0;
    const int scalar_rc = compute_match_exposure_scalars(raw_info,
                                                        raw_buffer_32,
                                                        dark_frame,
                                                        iso1,
                                                        iso2,
                                                        auto_correction,
                                                        ev_correction,
                                                        black_delta,
                                                        white_darkened,
                                                        is_bright,
                                                        scratch,
                                                        1,
                                                        &applied_ev_correction,
                                                        &applied_black_delta);
    g_dualiso_match_probe.valid = 1;
    g_dualiso_match_probe.rc = scalar_rc;
    g_dualiso_match_probe.mode = probe_mode;
    g_dualiso_match_probe.ev = scalar_rc > 0 ? applied_ev_correction : 0.0;
    g_dualiso_match_probe.black_delta = scalar_rc > 0 ? applied_black_delta : 0;
    g_dualiso_match_probe.black = black;
    g_dualiso_match_probe.white = raw_info.white_level;
    g_dualiso_match_probe.white_darkened = scalar_rc > 0 ? *white_darkened : 0;
    if (scalar_rc <= 0)
    {
        return scalar_rc;
    }

    const int _black_delta = applied_black_delta;
    const double _ev_correction = applied_ev_correction;
    double factor = pow(2, -_ev_correction);

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            int p = raw_get_pixel32(x, y);

            if (p == 0) continue;

            if (BRIGHT_ROW)
            {
                p = ((p - black + _black_delta) * factor) + black;
            }
            else
            {
                p = (p - _black_delta) + (_black_delta * factor);
            }

            raw_set_pixel20(x, y, p);
        }
    }

    return 1;
}

static inline uint32_t * convert_to_20bit(struct raw_info raw_info, uint16_t * image_data, uint32_t * raw_buffer_32)
{
    int w = raw_info.width;
    int h = raw_info.height;
    if (!raw_buffer_32)
    {
        return NULL;
    }

#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2)
    {
        /* Per-row dispatch so OMP parallelism still applies. */
        #pragma omp parallel for
        for (int y = 0; y < h; y ++) {
            convert_to_20bit_avx2(&raw_buffer_32[(size_t)y*w],
                                  &image_data[(size_t)y*w],
                                  (size_t)w);
        }
        return raw_buffer_32;
    }
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
        for (int x = 0; x < w; x ++)
            raw_buffer_32[x + y*w] = raw_get_pixel_14to20(x, y);

    return raw_buffer_32;
}

static inline void build_ev2raw_lut(int * raw2ev, int * ev2raw_0, int black, int white)
{
    int* ev2raw = ev2raw_0 + 10*EV_RESOLUTION;
    
    #pragma omp parallel for
    for (int i = 0; i < 1<<20; i++)
    {
        double signal = MAX(i/64.0 - black/64.0, -1023);
        if (signal > 0)
            raw2ev[i] = (int)round(log2(1+signal) * EV_RESOLUTION);
        else
            raw2ev[i] = -(int)round(log2(1-signal) * EV_RESOLUTION);
    }
    
    #pragma omp parallel for
    for (int i = -10*EV_RESOLUTION; i < 0; i++)
    {
        ev2raw[i] = COERCE(black+64 - round(64*pow(2, ((double)-i/EV_RESOLUTION))), 0, black);
    }
    
    #pragma omp parallel for
    for (int i = 0; i < 14*EV_RESOLUTION; i++)
    {
        ev2raw[i] = COERCE(black-64 + round(64*pow(2, ((double)i/EV_RESOLUTION))), black, (1<<20)-1);
        
        if (i >= raw2ev[white])
        {
            ev2raw[i] = MAX(ev2raw[i], white);
        }
    }
    
    /* keep "bad" pixels, if any */
    ev2raw[raw2ev[0]] = 0;
    ev2raw[raw2ev[0]] = 0;
    
    /* check raw <--> ev conversion */
    //~ printf("%d %d %d %d %d %d %d *%d* %d %d %d %d %d\n", raw2ev[0],         raw2ev[16000],         raw2ev[32000],         raw2ev[131068],         raw2ev[131069],         raw2ev[131070],         raw2ev[131071],         raw2ev[131072],         raw2ev[131073],         raw2ev[131074],         raw2ev[131075],         raw2ev[131076],         raw2ev[132000]);
    //~ printf("%d %d %d %d %d %d %d *%d* %d %d %d %d %d\n", ev2raw[raw2ev[0]], ev2raw[raw2ev[16000]], ev2raw[raw2ev[32000]], ev2raw[raw2ev[131068]], ev2raw[raw2ev[131069]], ev2raw[raw2ev[131070]], ev2raw[raw2ev[131071]], ev2raw[raw2ev[131072]], ev2raw[raw2ev[131073]], ev2raw[raw2ev[131074]], ev2raw[raw2ev[131075]], ev2raw[raw2ev[131076]], ev2raw[raw2ev[132000]]);
}

static int ensure_scratch_ev_lut(dualiso_full20bit_scratch_t * scratch,
                                 int black,
                                 int white,
                                 int ** raw2ev_out,
                                 float ** raw2ev_float_out,
                                 int ** ev2raw_out)
{
    if (!scratch || !raw2ev_out || !raw2ev_float_out || !ev2raw_out)
    {
        return 0;
    }

    int * raw2ev = ensure_reusable_scratch_buffer((void **)&scratch->ev_raw2ev,
                                                  &scratch->ev_raw2ev_capacity,
                                                  DUALISO_RAW2EV_LUT_COUNT,
                                                  sizeof(*scratch->ev_raw2ev))
        ? scratch->ev_raw2ev
        : NULL;
    float * raw2ev_float = ensure_reusable_scratch_buffer((void **)&scratch->ev_raw2ev_float,
                                                          &scratch->ev_raw2ev_float_capacity,
                                                          DUALISO_RAW2EV_LUT_COUNT,
                                                          sizeof(*scratch->ev_raw2ev_float))
        ? scratch->ev_raw2ev_float
        : NULL;
    int * ev2raw_0 = ensure_reusable_scratch_buffer((void **)&scratch->ev2raw_0,
                                                    &scratch->ev2raw_capacity,
                                                    DUALISO_EV2RAW_LUT_COUNT,
                                                    sizeof(*scratch->ev2raw_0))
        ? scratch->ev2raw_0
        : NULL;
    if (!raw2ev || !raw2ev_float || !ev2raw_0)
    {
        return 0;
    }

    if (!scratch->ev_lut_valid
        || scratch->ev_lut_black != black
        || scratch->ev_lut_white != white
        || !scratch->ev_lut_float_valid)
    {
        build_ev2raw_lut(raw2ev, ev2raw_0, black, white);
        #pragma omp parallel for
        for (int i = 0; i < (int)DUALISO_RAW2EV_LUT_COUNT; i++)
        {
            raw2ev_float[i] = (float)raw2ev[i];
        }
        scratch->ev_lut_black = black;
        scratch->ev_lut_white = white;
        scratch->ev_lut_valid = 1;
        scratch->ev_lut_float_valid = 1;
    }

    *raw2ev_out = raw2ev;
    *raw2ev_float_out = raw2ev_float;
    *ev2raw_out = ev2raw_0 + 10 * EV_RESOLUTION;
    return 1;
}

static inline double compute_noise(struct raw_info raw_info, uint16_t * image_data, double * noise_std, double * dark_noise, double * bright_noise, double * dark_noise_ev, double * bright_noise_ev)
{
    double noise_avg = 0.0;
    int has_noise_samples = 0;
    for (int y = 0; y < 4; y++)
    {
        int y1 = raw_info.active_area.y1/4*4 + 20 + y;
        int y2 = raw_info.active_area.y2 - 20;
        if (8 < raw_info.active_area.x1 - 8 && y1 < y2)
        {
            has_noise_samples = 1;
            break;
        }
    }
    if (has_noise_samples)
    {
        for (int y = 0; y < 4; y++)
            compute_black_noise(raw_info, image_data, 8, raw_info.active_area.x1 - 8, raw_info.active_area.y1/4*4 + 20 + y, raw_info.active_area.y2 - 20, 1, 4, &noise_avg, &noise_std[y]);
    }
    else
    {
        noise_avg = raw_info.black_level;
        for (int y = 0; y < 4; y++)
            noise_std[y] = 8.0; /* compute_black_noise's empty-window default */
    }
#ifndef STDOUT_SILENT
    printf("Noise levels    : %.02f %.02f %.02f %.02f (14-bit)\n", noise_std[0], noise_std[1], noise_std[2], noise_std[3]);
#endif
    *dark_noise = MIN(MIN(noise_std[0], noise_std[1]), MIN(noise_std[2], noise_std[3]));
    *bright_noise = MAX(MAX(noise_std[0], noise_std[1]), MAX(noise_std[2], noise_std[3]));
    *dark_noise_ev = log2(*dark_noise);
    *bright_noise_ev = log2(*bright_noise);
    return noise_avg;
}

static inline double compute_fullres_curve_value(int black, int i)
{
    const double fullres_start = 4;
    const double fullres_transition = 4;
    double ev2 = log2(MAX(i/64.0 - black/64.0, 1));
    double c2 = -cos(COERCE(ev2 - fullres_start, 0, fullres_transition)*M_PI/fullres_transition);
    return (c2+1) / 2;
}

static inline double * build_fullres_curve(dualiso_full20bit_scratch_t * scratch, int black)
{
    if(!scratch) return NULL;

    const size_t curve_count = (size_t)(1u << 20);
    if(scratch->fullres_curve
       && scratch->fullres_curve_capacity >= curve_count
       && scratch->fullres_curve_valid
       && scratch->fullres_curve_black == black)
    {
        return scratch->fullres_curve;
    }

    double * fullres_curve = ensure_double_scratch_buffer(&scratch->fullres_curve,
                                                          &scratch->fullres_curve_capacity,
                                                          curve_count);
    if(!fullres_curve) return NULL;

    scratch->fullres_curve_valid = 0;
    #pragma omp parallel for
    for (int i = 0; i < (1<<20); i++)
    {
        fullres_curve[i] = compute_fullres_curve_value(black, i);
    }

    scratch->fullres_curve_black = black;
    scratch->fullres_curve_valid = 1;
    return fullres_curve;
}

static inline float * build_fullres_curve_float(dualiso_full20bit_scratch_t * scratch, int black)
{
    if(!scratch) return NULL;

    const size_t curve_count = (size_t)(1u << 20);
    if(scratch->fullres_curve_float
       && scratch->fullres_curve_float_capacity >= curve_count
       && scratch->fullres_curve_float_valid
       && scratch->fullres_curve_float_black == black)
    {
        return scratch->fullres_curve_float;
    }

    float * fullres_curve_f = ensure_float_scratch_buffer(&scratch->fullres_curve_float,
                                                          &scratch->fullres_curve_float_capacity,
                                                          curve_count);
    if(!fullres_curve_f) return NULL;

    scratch->fullres_curve_float_valid = 0;
    scratch->fullres_curve_float_as_double_valid = 0;
    #pragma omp parallel for
    for (int i = 0; i < (1<<20); i++)
    {
        fullres_curve_f[i] = (float)compute_fullres_curve_value(black, i);
    }

    scratch->fullres_curve_float_black = black;
    scratch->fullres_curve_float_valid = 1;
    return fullres_curve_f;
}

static inline double * build_fullres_curve_float_as_double(dualiso_full20bit_scratch_t * scratch, int black)
{
    if(!scratch) return NULL;

    const size_t curve_count = (size_t)(1u << 20);
    if(scratch->fullres_curve_float_as_double
       && scratch->fullres_curve_float_as_double_capacity >= curve_count
       && scratch->fullres_curve_float_as_double_valid
       && scratch->fullres_curve_float_as_double_black == black)
    {
        return scratch->fullres_curve_float_as_double;
    }

    double * fullres_curve_fd =
        ensure_double_scratch_buffer(&scratch->fullres_curve_float_as_double,
                                     &scratch->fullres_curve_float_as_double_capacity,
                                     curve_count);
    float * fullres_curve_f = build_fullres_curve_float(scratch, black);
    if(!fullres_curve_fd || !fullres_curve_f) return NULL;

    scratch->fullres_curve_float_as_double_valid = 0;
    #pragma omp parallel for
    for(int i = 0; i < 1<<20; i++)
    {
        fullres_curve_fd[i] = (double)fullres_curve_f[i];
    }

    scratch->fullres_curve_float_as_double_black = black;
    scratch->fullres_curve_float_as_double_valid = 1;
    return fullres_curve_fd;
}

static inline int use_final_blend_float_fullres_curve(void)
{
    return !dualiso_env_truthy(getenv("MLVAPP_DISABLE_DUALISO_FLOAT_FULLRES_CURVE"));
}

/* define edge directions for interpolation */
struct xy { int x; int y; };
const struct
{
    struct xy ack;      /* verification pixel near a */
    struct xy a;        /* interpolation pixel from the nearby line: normally (0,s) but also (1,s) or (-1,s) */
    struct xy b;        /* interpolation pixel from the other line: normally (0,-2s) but also (1,-2s), (-1,-2s), (2,-2s) or (-2,-2s) */
    struct xy bck;      /* verification pixel near b */
}
edge_directions[] = {       /* note: all y coords should be multiplied by s */
    //~ { {-6,2}, {-3,1}, { 6,-2}, { 9,-3} },     /* almost horizontal (little or no improvement) */
    { {-4,2}, {-2,1}, { 4,-2}, { 6,-3} },
    { {-3,2}, {-1,1}, { 3,-2}, { 4,-3} },
    { {-2,2}, {-1,1}, { 2,-2}, { 3,-3} },     /* 45-degree diagonal */
    { {-1,2}, {-1,1}, { 1,-2}, { 2,-3} },
    { {-1,2}, { 0,1}, { 1,-2}, { 1,-3} },
    { { 0,2}, { 0,1}, { 0,-2}, { 0,-3} },     /* vertical, preferred; no extra confirmations needed */
    { { 1,2}, { 0,1}, {-1,-2}, {-1,-3} },
    { { 1,2}, { 1,1}, {-1,-2}, {-2,-3} },
    { { 2,2}, { 1,1}, {-2,-2}, {-3,-3} },     /* 45-degree diagonal */
    { { 3,2}, { 1,1}, {-3,-2}, {-4,-3} },
    { { 4,2}, { 2,1}, {-4,-2}, {-6,-3} },
    //~ { { 6,2}, { 3,1}, {-6,-2}, {-9,-3} },     /* almost horizontal */
};

/* Phase E1 — AMaZE edge-direction estimator AVX2 kernel.
 *
 * Included here (rather than at the top with dualiso_avx2.inc) because the
 * kernel needs the file-scope `edge_directions[]` table just declared above.
 * Wired via the g_dualiso_amaze_use_avx2 dispatch flag declared earlier in
 * this TU. */
#include "dualiso_amaze_avx2.inc"

static inline int edge_interp(float ** plane, int * squeezed, int * raw2ev, int dir, int x, int y, int s)
{
    
    int dxa = edge_directions[dir].a.x;
    int dya = edge_directions[dir].a.y * s;
    int pa = COERCE((int)plane[squeezed[y+dya]][x+dxa], 0, 0xFFFFF);
    int dxb = edge_directions[dir].b.x;
    int dyb = edge_directions[dir].b.y * s;
    int pb = COERCE((int)plane[squeezed[y+dyb]][x+dxb], 0, 0xFFFFF);
    int pi = (raw2ev[pa] * 2 + raw2ev[pb]) / 3;
    
    return pi;
}

typedef struct
{
    amazeinfo_t* info;
    double* elapsed_ms;
    int* succeeded;
} amaze_demosaic_thread_arg_t;

static int g_dualiso_amaze_thread_create_failure_for_testing = -1;

void dualisoSetAmazeThreadCreateFailureForTesting(int thread_index);
void dualisoSetAmazeThreadCreateFailureForTesting(int thread_index)
{
    g_dualiso_amaze_thread_create_failure_for_testing = thread_index;
}

static void* demosaic_wrapper(void* arg) {
    amaze_demosaic_thread_arg_t* thread_arg = (amaze_demosaic_thread_arg_t*)arg;
    double start = mlv_stage_timing_now();
    const int succeeded = demosaic(thread_arg->info);
    if (thread_arg->succeeded)
        *thread_arg->succeeded = succeeded;
    if (thread_arg->elapsed_ms)
        *thread_arg->elapsed_ms = dualiso_debug_elapsed_ms(start);
    return NULL;
}

static inline int amaze_interpolate(struct raw_info raw_info,
                                     uint32_t * raw_buffer_32,
                                     uint32_t* dark,
                                     uint32_t* bright,
                                     int black,
                                     int white,
                                     int white_darkened,
                                     int * is_bright,
                                     int threads,
                                     dualiso_full20bit_scratch_t * scratch)
{
    int w = raw_info.width;
    int h = raw_info.height;
    int wx = w + 16;

    double amaze_stage_start = mlv_stage_timing_now();
    if (!ensure_amaze_interpolation_scratch(scratch, (size_t)h, (size_t)wx, (size_t)w * (size_t)h, (size_t)threads))
    {
        g_dualiso_full20bit_timing.interp_amaze_scratch_ms +=
            dualiso_debug_elapsed_ms(amaze_stage_start);
        return 0;
    }
    g_dualiso_full20bit_timing.interp_amaze_scratch_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    int* squeezed = scratch->amaze_squeezed;
    float** rawData = scratch->amaze_rawData_rows;
    float** red = scratch->amaze_red_rows;
    float** green = scratch->amaze_green_rows;
    float** blue = scratch->amaze_blue_rows;
    amaze_stage_start = mlv_stage_timing_now();
    for (int y = 0; y < h; y++)
        squeezed[y] = -1;
    memset(scratch->amaze_rawData_storage, 0, (size_t)h * (size_t)wx * sizeof(float));
    g_dualiso_full20bit_timing.interp_amaze_clear_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);
    
    /* squeeze the dark image by deleting fields from the bright exposure */
    amaze_stage_start = mlv_stage_timing_now();
    int yh = -1;
    for (int y = 0; y < h; y ++)
    {
        if (BRIGHT_ROW)
            continue;
        
        if (yh < 0) /* make sure we start at the same parity (RGGB cell) */
            yh = y;

        squeezed[y] = yh;
        yh++;
    }
    
    /* now the same for the bright exposure */
    yh = -1;
    for (int y = 0; y < h; y ++)
    {
        if (!BRIGHT_ROW)
            continue;
        
        if (yh < 0) /* make sure we start with the same parity (RGGB cell) */
            yh = h/4*2 + y;

        squeezed[y] = yh;
        yh++;
        if (yh >= h) break; /* just in case */
    }
    g_dualiso_full20bit_timing.interp_amaze_squeeze_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    amaze_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for
    for (int y = 0; y < h; y ++)
    {
        if (squeezed[y] < 0)
            continue;

        const uint32_t * src_row = raw_buffer_32 + (size_t)y * (size_t)w;
        float * raw_row = rawData[squeezed[y]];
        for (int x = 0; x < w; x++)
        {
            int p = src_row[x];

            if (x%2 != y%2) /* divide green channel by 2 to approximate the final WB better */
                p = (p - black) / 2 + black;

            raw_row[x] = p;
        }
    }
    g_dualiso_full20bit_timing.interp_amaze_rawdata_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    amaze_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for
    for (int y = 0; y < h; y ++)
    {
        if (squeezed[y] < 0)
            squeezed[y] = 0;
    }
    g_dualiso_full20bit_timing.interp_amaze_squeeze_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    // Multithreaded debayer
    double demosaic_stage_start = mlv_stage_timing_now();
    amaze_stage_start = demosaic_stage_start;
    int* startchunk_y = scratch->amaze_startchunk_y;
    int* endchunk_y = scratch->amaze_endchunk_y;

    int chunk_height = h / threads;
    chunk_height -= chunk_height % 2;

    while(chunk_height <= 32 && threads > 1) {
        threads--;
        chunk_height = h / threads;
        chunk_height -= chunk_height % 2;
    }

    for (int thread = 0; thread < threads; ++thread) {
        startchunk_y[thread] = chunk_height * thread;
        endchunk_y[thread] = chunk_height * (thread + 1);
    }
    endchunk_y[threads-1] = h;

    pthread_t* thread_id = (pthread_t*)scratch->amaze_thread_id;
    amazeinfo_t* amaze_arguments = (amazeinfo_t*)scratch->amaze_arguments;
    const int requested_demosaic_threads = threads;
    int created_demosaic_threads = 0;
    int demosaic_thread_creation_failed = 0;
    double demosaic_worker_ms[threads];
    int demosaic_worker_succeeded[threads];
    amaze_demosaic_thread_arg_t demosaic_thread_args[threads];

    for (int thread = 0; thread < threads; ++thread) {
        demosaic_worker_ms[thread] = 0.0;
        demosaic_worker_succeeded[thread] = 0;
    }
    g_dualiso_full20bit_timing.interp_amaze_demosaic_setup_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    amaze_stage_start = mlv_stage_timing_now();
    for (int thread = 0; thread < threads; ++thread) {
        amaze_arguments[thread] = (amazeinfo_t) {
            rawData,
            red,
            green,
            blue,
            0, startchunk_y[thread],
            w, (endchunk_y[thread] - startchunk_y[thread]),
            0,
            0
        };
        demosaic_thread_args[thread] = (amaze_demosaic_thread_arg_t) {
            &amaze_arguments[thread],
            &demosaic_worker_ms[thread],
            &demosaic_worker_succeeded[thread]
        };

        const int create_result =
            g_dualiso_amaze_thread_create_failure_for_testing == thread
            ? 1
            : pthread_create(&thread_id[thread],
                             NULL,
                             demosaic_wrapper,
                             &demosaic_thread_args[thread]);
        if (create_result != 0)
        {
            demosaic_thread_creation_failed = 1;
            break;
        }
        created_demosaic_threads++;
    }
    g_dualiso_full20bit_timing.interp_amaze_demosaic_create_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    amaze_stage_start = mlv_stage_timing_now();
    for (int thread = 0; thread < created_demosaic_threads; ++thread) {
        pthread_join(thread_id[thread], NULL);
    }
    g_dualiso_full20bit_timing.interp_amaze_demosaic_join_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);

    if (demosaic_thread_creation_failed
        || created_demosaic_threads != requested_demosaic_threads)
        return 0;
    for (int thread = 0; thread < created_demosaic_threads; ++thread)
    {
        if (!demosaic_worker_succeeded[thread])
            return 0;
    }

    double demosaic_worker_total_ms = 0.0;
    double demosaic_worker_max_ms = 0.0;
    for (int thread = 0; thread < created_demosaic_threads; ++thread) {
        demosaic_worker_total_ms += demosaic_worker_ms[thread];
        if (demosaic_worker_ms[thread] > demosaic_worker_max_ms)
            demosaic_worker_max_ms = demosaic_worker_ms[thread];
    }
    g_dualiso_full20bit_timing.interp_amaze_demosaic_worker_total_ms +=
        demosaic_worker_total_ms;
    g_dualiso_full20bit_timing.interp_amaze_demosaic_worker_max_ms +=
        demosaic_worker_max_ms;
    g_dualiso_full20bit_timing.interp_amaze_demosaic_worker_count +=
        created_demosaic_threads;
    g_dualiso_full20bit_timing.interp_amaze_demosaic_ms +=
        dualiso_debug_elapsed_ms(demosaic_stage_start);

    /* undo green channel scaling and clamp the other channels */
    amaze_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            green[y][x] = COERCE((green[y][x] - black) * 2 + black, 0, 0xFFFFF);
            red[y][x] = COERCE(red[y][x], 0, 0xFFFFF);
            blue[y][x] = COERCE(blue[y][x], 0, 0xFFFFF);
        }
    }
    g_dualiso_full20bit_timing.interp_amaze_postprocess_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);
#ifndef STDOUT_SILENT
    printf("Edge-directed interpolation...\n");
#endif
    //~ printf("Grayscale...\n");
    /* convert to grayscale and de-squeeze for easier processing */
    uint32_t * gray = scratch->amaze_gray;

    amaze_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
        for (int x = 0; x < w; x ++)
            gray[x + y*w] = green[squeezed[y]][x]/2 + red[squeezed[y]][x]/4 + blue[squeezed[y]][x]/4;
    g_dualiso_full20bit_timing.interp_amaze_grayscale_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);
    
    
    uint8_t* edge_direction = scratch->amaze_edge_direction;
    int d0 = COUNT(edge_directions)/2;

    amaze_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
        for (int x = 0; x < w; x ++)
            edge_direction[x + y*w] = d0;
    
    double * fullres_curve = build_fullres_curve(scratch, black);
    if(!fullres_curve)
    {
        g_dualiso_full20bit_timing.interp_amaze_edge_init_ms +=
            dualiso_debug_elapsed_ms(amaze_stage_start);
        return 0;
    }
    g_dualiso_full20bit_timing.interp_amaze_edge_init_ms +=
        dualiso_debug_elapsed_ms(amaze_stage_start);
    
    //~ printf("Cross-correlation...\n");
    
    /* for fast EV - raw conversion */
    static int raw2ev[1<<20];   /* EV x EV_RESOLUTION */
    static int ev2raw_0[24*EV_RESOLUTION];
    static int previous_black = -1;
    
    /* handle sub-black values (negative EV) */
    int* ev2raw = ev2raw_0 + 10*EV_RESOLUTION;
    
    LOCK(ev2raw_mutex)
    {
        int semi_overexposed = 0;
        int not_overexposed = 0;
        int deep_shadow = 0;
        int not_shadow = 0;
        uint64_t edge_simd_batches = 0;
        uint64_t edge_allskip_batches = 0;
        uint64_t edge_mixed_batches = 0;
        uint64_t edge_fullsearch_batches = 0;
        uint64_t edge_fullsearch_pixels = 0;
        uint64_t edge_skip_pixels = 0;
        uint64_t edge_scalar_pixels = 0;

        amaze_stage_start = mlv_stage_timing_now();
        if(black != previous_black)
        {
            build_ev2raw_lut(raw2ev, ev2raw_0, black, white);
            previous_black = black;
        }
        g_dualiso_full20bit_timing.interp_amaze_lut_ms +=
            dualiso_debug_elapsed_ms(amaze_stage_start);
#ifdef DUALISO_AVX2_AVAILABLE
        pthread_once(&g_dualiso_amaze_dispatch_once, dualiso_amaze_dispatch_init);
        amaze_stage_start = mlv_stage_timing_now();
        if (g_dualiso_amaze_use_avx2)
        {
            /* Phase E1 — AVX2 fast path. Skips the diagnostic stat counters
             * (semi_overexposed/not_overexposed/deep_shadow/not_shadow) which
             * only feed the STDOUT_SILENT printfs below. The kernel is
             * byte-identical for `edge_direction[x + y*w]` writes. */
            #pragma omp parallel for
            for (int y = 5; y < h-5; y ++)
            {
                int s = (is_bright[y%4] == is_bright[(y+1)%4]) ? -1 : 1;
                int br = BRIGHT_ROW;
                uint64_t row_simd_batches = 0;
                uint64_t row_allskip_batches = 0;
                uint64_t row_mixed_batches = 0;
                uint64_t row_fullsearch_batches = 0;
                uint64_t row_fullsearch_pixels = 0;
                uint64_t row_skip_pixels = 0;
                uint64_t row_scalar_pixels = 0;
                /* Pass a row pointer into the per-row kernel so its writes
                 * land in edge_direction[x + y*w]. */
                amaze_edge_direction_estimator_row_avx2(
                    y, w, s, br,
                    gray, raw_buffer_32, raw2ev,
                    fullres_curve, fullres_thr,
                    (uint32_t)white_darkened,
                    &edge_direction[(size_t)y * (size_t)w],
                    &row_simd_batches,
                    &row_allskip_batches,
                    &row_mixed_batches,
                    &row_fullsearch_batches,
                    &row_fullsearch_pixels,
                    &row_skip_pixels,
                    &row_scalar_pixels);
#pragma omp atomic
                edge_simd_batches += row_simd_batches;
#pragma omp atomic
                edge_allskip_batches += row_allskip_batches;
#pragma omp atomic
                edge_mixed_batches += row_mixed_batches;
#pragma omp atomic
                edge_fullsearch_batches += row_fullsearch_batches;
#pragma omp atomic
                edge_fullsearch_pixels += row_fullsearch_pixels;
#pragma omp atomic
                edge_skip_pixels += row_skip_pixels;
#pragma omp atomic
                edge_scalar_pixels += row_scalar_pixels;
            }
        }
        else
#endif
        {
#ifndef DUALISO_AVX2_AVAILABLE
        amaze_stage_start = mlv_stage_timing_now();
#endif
        #pragma omp parallel for
        for (int y = 5; y < h-5; y ++)
        {
            int s = (is_bright[y%4] == is_bright[(y+1)%4]) ? -1 : 1;    /* points to the closest row having different exposure */
            for (int x = 5; x < w-5; x ++)
            {
                int e_best = INT_MAX;
                int d_best = d0;
                int dmin = 0;
                int dmax = COUNT(edge_directions)-1;
                int search_area = 5;

                /* only use high accuracy on the dark exposure where the bright ISO is overexposed */
                if (!BRIGHT_ROW)
                {
                    /* interpolating bright exposure */
                    if (fullres_curve[raw_get_pixel32(x, y)] > fullres_thr)
                    {
#pragma omp atomic
                        /* no high accuracy needed, just interpolate vertically */
                        not_shadow++;
                        dmin = d0;
                        dmax = d0;
                    }
                    else
                    {
#pragma omp atomic
                        /* deep shadows, unlikely to use fullres, so we need a good interpolation */
                        deep_shadow++;
                    }
                }
                else if (raw_get_pixel32(x, y) < (unsigned int)white_darkened)
                {
#pragma omp atomic
                    /* interpolating dark exposure, but we also have good data from the bright one */
                    not_overexposed++;
                    dmin = d0;
                    dmax = d0;
                }
                else
                {
#pragma omp atomic
                    /* interpolating dark exposure, but the bright one is clipped */
                    semi_overexposed++;
                }

                if (dmin == dmax)
                {
#pragma omp atomic
                    edge_skip_pixels++;
                }
                else
                {
#pragma omp atomic
                    edge_fullsearch_pixels++;
                }
#pragma omp atomic
                edge_scalar_pixels++;

                if (dmin == dmax)
                {
                    d_best = dmin;
                }
                else
                {
                    for (int d = dmin; d <= dmax; d++)
                    {
                        int e = 0;
                        for (int j = -search_area; j <= search_area; j++)
                        {
                            int dx1 = edge_directions[d].ack.x + j;
                            int dy1 = edge_directions[d].ack.y * s;
                            int p1 = raw2ev[gray[x+dx1 + (y+dy1)*w]];
                            int dx2 = edge_directions[d].a.x + j;
                            int dy2 = edge_directions[d].a.y * s;
                            int p2 = raw2ev[gray[x+dx2 + (y+dy2)*w]];
                            int dx3 = edge_directions[d].b.x + j;
                            int dy3 = edge_directions[d].b.y * s;
                            int p3 = raw2ev[gray[x+dx3 + (y+dy3)*w]];
                            int dx4 = edge_directions[d].bck.x + j;
                            int dy4 = edge_directions[d].bck.y * s;
                            int p4 = raw2ev[gray[x+dx4 + (y+dy4)*w]];
                            e += ABS(p1-p2) + ABS(p2-p3) + ABS(p3-p4);
                        }

                        /* add a small penalty for diagonal directions */
                        /* (the improvement should be significant in order to choose one of these) */
                        e += ABS(d - d0) * EV_RESOLUTION/8;

                        if (e < e_best)
                        {
                            e_best = e;
                            d_best = d;
                        }
                    }
                }

                edge_direction[x + y*w] = d_best;
            }
        }
        }
        g_dualiso_full20bit_timing.interp_amaze_edge_direction_ms +=
            dualiso_debug_elapsed_ms(amaze_stage_start);
        g_dualiso_full20bit_timing.interp_amaze_edge_simd_batches +=
            (double)edge_simd_batches;
        g_dualiso_full20bit_timing.interp_amaze_edge_allskip_batches +=
            (double)edge_allskip_batches;
        g_dualiso_full20bit_timing.interp_amaze_edge_mixed_batches +=
            (double)edge_mixed_batches;
        g_dualiso_full20bit_timing.interp_amaze_edge_fullsearch_batches +=
            (double)edge_fullsearch_batches;
        g_dualiso_full20bit_timing.interp_amaze_edge_fullsearch_pixels +=
            (double)edge_fullsearch_pixels;
        g_dualiso_full20bit_timing.interp_amaze_edge_skip_pixels +=
            (double)edge_skip_pixels;
        g_dualiso_full20bit_timing.interp_amaze_edge_scalar_pixels +=
            (double)edge_scalar_pixels;
#ifndef STDOUT_SILENT
        printf("Semi-overexposed: %.02f%%\n", semi_overexposed * 100.0 / (semi_overexposed + not_overexposed));
        printf("Deep shadows    : %.02f%%\n", deep_shadow * 100.0 / (deep_shadow + not_shadow));
#endif
        //~ printf("Actual interpolation...\n");
        
        amaze_stage_start = mlv_stage_timing_now();
        #pragma omp parallel for
        for (int y = 2; y < h-2; y ++)
        {
            uint32_t* native = BRIGHT_ROW ? bright : dark;
            uint32_t* interp = BRIGHT_ROW ? dark : bright;
            int is_rg = (y % 2 == 0); /* RG or GB? */
            int s = (is_bright[y%4] == is_bright[(y+1)%4]) ? -1 : 1;    /* points to the closest row having different exposure */
            
            //~ printf("Interpolating %s line %d from [near] %d (squeezed %d) and [far] %d (squeezed %d)\n", BRIGHT_ROW ? "BRIGHT" : "DARK", y, y+s, yh_near, y-2*s, yh_far);
            
            for (int x = 2; x < w-2; x += 2)
            {
                for (int k = 0; k < 2; k++, x++)
                {
                    float** plane = is_rg ? (x%2 == 0 ? red   : green)
                    : (x%2 == 0 ? green : blue );
                    
                    int dir = edge_direction[x + y*w];
                    
                    /* vary the interpolation direction and average the result (reduces aliasing) */
                    int pi0 = edge_interp(plane, squeezed, raw2ev, dir, x, y, s);
                    int pip = edge_interp(plane, squeezed, raw2ev, MIN(dir+1, COUNT(edge_directions)-1), x, y, s);
                    int pim = edge_interp(plane, squeezed, raw2ev, MAX(dir-1,0), x, y, s);
                    
                    interp[x   + y * w] = ev2raw[(2*pi0+pip+pim)/4];
                    native[x   + y * w] = raw_get_pixel32(x, y);
                }
                x -= 2;
            }
        }
        g_dualiso_full20bit_timing.interp_amaze_actual_interp_ms +=
            dualiso_debug_elapsed_ms(amaze_stage_start);
    }
    UNLOCK(ev2raw_mutex)

    return 1;
}

static inline void mean23_interpolate_with_lut(struct raw_info raw_info,
                                               uint32_t * raw_buffer_32,
                                               uint32_t* dark,
                                               uint32_t* bright,
                                               int white_darkened,
                                               int * is_bright,
                                               int * raw2ev,
                                               int * ev2raw)
{
    int w = raw_info.width;
    int h = raw_info.height;
    #pragma omp parallel for
    for (int y = 2; y < h-2; y ++)
    {
        const int bright_row = BRIGHT_ROW;
        const int row_offset = y * w;
        const uint32_t * const raw_row = raw_buffer_32 + row_offset;
        const uint32_t * const raw_y_minus_2 = raw_buffer_32 + (y - 2) * w;
        const uint32_t * const raw_y_plus_2 = raw_buffer_32 + (y + 2) * w;
        uint32_t* native = (bright_row ? bright : dark) + row_offset;
        uint32_t* interp = (bright_row ? dark : bright) + row_offset;
        int is_rg = (y % 2 == 0); /* RG or GB? */
        int row_white_ev = raw2ev[bright_row ? raw_info.white_level : white_darkened];
        int s = (is_bright[y%4] == is_bright[(y+1)%4]) ? -1 : 1;
        const uint32_t * const raw_y_s = raw_buffer_32 + (y + s) * w;
        const uint32_t * const raw_y_far = raw_buffer_32 + (y - 2 * s) * w;

        if (is_rg)
        {
            for (int x = 2; x < w-3; x += 2)
            {
                /* red/blue: interpolate from (x,y+2) and (x,y-2) */
                int ri = mean2_noerr(raw2ev[raw_y_minus_2[x]],
                                      raw2ev[raw_y_plus_2[x]],
                                      row_white_ev);

                /* green: interpolate from the neighboring same-brightness row. */
                int gi = mean3_noerr(raw2ev[raw_y_s[x + 2]],
                                      raw2ev[raw_y_s[x]],
                                      raw2ev[raw_y_far[x + 1]],
                                      row_white_ev);

                interp[x] = ev2raw[ri];
                interp[x+1] = ev2raw[gi];
                native[x] = raw_row[x];
                native[x+1] = raw_row[x+1];
            }
        }
        else
        {
            for (int x = 2; x < w-3; x += 2)
            {
                int bi = mean2_noerr(raw2ev[raw_y_minus_2[x + 1]],
                                      raw2ev[raw_y_plus_2[x + 1]],
                                      row_white_ev);

                int gi = mean3_noerr(raw2ev[raw_y_s[x + 1]],
                                      raw2ev[raw_y_s[x - 1]],
                                      raw2ev[raw_y_far[x]],
                                      row_white_ev);

                interp[x] = ev2raw[gi];
                interp[x+1] = ev2raw[bi];
                native[x] = raw_row[x];
                native[x+1] = raw_row[x+1];
            }
        }
    }
}

static inline void mean23_interpolate(struct raw_info raw_info, uint32_t * raw_buffer_32, uint32_t* dark, uint32_t* bright, int black, int white, int white_darkened, int * is_bright, dualiso_full20bit_scratch_t * scratch)
{
#ifndef STDOUT_SILENT
    printf("Interpolation   : mean23\n");
#endif
    int * scratch_raw2ev = NULL;
    int * scratch_ev2raw = NULL;
    float * scratch_raw2ev_float = NULL;
    if (ensure_scratch_ev_lut(scratch, black, white, &scratch_raw2ev, &scratch_raw2ev_float, &scratch_ev2raw))
    {
        mean23_interpolate_with_lut(raw_info,
                                    raw_buffer_32,
                                    dark,
                                    bright,
                                    white_darkened,
                                    is_bright,
                                    scratch_raw2ev,
                                    scratch_ev2raw);
        return;
    }

    /* Fallback for callers without reusable scratch storage. */
    static int raw2ev[1<<20];   /* EV x EV_RESOLUTION */
    static int ev2raw_0[24*EV_RESOLUTION];
    static int previous_black = -1;
    
    int* ev2raw = ev2raw_0 + 10*EV_RESOLUTION;
    
    LOCK(ev2raw_mutex)
    {
        if(black != previous_black)
        {
            build_ev2raw_lut(raw2ev, ev2raw_0, black, white);
            previous_black = black;
        }
        mean23_interpolate_with_lut(raw_info,
                                    raw_buffer_32,
                                    dark,
                                    bright,
                                    white_darkened,
                                    is_bright,
                                    raw2ev,
                                    ev2raw);
    }
    UNLOCK(ev2raw_mutex)
}

static inline void border_interpolate(struct raw_info raw_info, uint32_t * raw_buffer_32, uint32_t* dark, uint32_t* bright, int * is_bright)
{
    int w = raw_info.width;
    int h = raw_info.height;
    
    /* border interpolation */
    for (int y = 0; y < 3; y ++)
    {
        uint32_t* native = BRIGHT_ROW ? bright : dark;
        uint32_t* interp = BRIGHT_ROW ? dark : bright;
        
        for (int x = 0; x < w; x ++)
        {
            interp[x + y * w] = raw_get_pixel32(x, y+2);
            native[x + y * w] = raw_get_pixel32(x, y);
        }
    }
    
    for (int y = h-4; y < h; y ++)
    {
        uint32_t* native = BRIGHT_ROW ? bright : dark;
        uint32_t* interp = BRIGHT_ROW ? dark : bright;
        
        for (int x = 0; x < w; x ++)
        {
            interp[x + y * w] = raw_get_pixel32(x, y-2);
            native[x + y * w] = raw_get_pixel32(x, y);
        }
    }
    
    for (int y = 2; y < h; y ++)
    {
        uint32_t* native = BRIGHT_ROW ? bright : dark;
        uint32_t* interp = BRIGHT_ROW ? dark : bright;
        
        for (int x = 0; x < 2; x ++)
        {
            interp[x + y * w] = raw_get_pixel32(x, y-2);
            native[x + y * w] = raw_get_pixel32(x, y);
        }
        
        for (int x = w-3; x < w; x ++)
        {
            interp[x + y * w] = raw_get_pixel32(x-2, y-2);
            native[x + y * w] = raw_get_pixel32(x-2, y);
        }
    }
}

static inline void fullres_reconstruction(struct raw_info raw_info, uint32_t * fullres, uint32_t* dark, uint32_t* bright, uint32_t white_darkened, int * is_bright)
{
    int w = raw_info.width;
    int h = raw_info.height;

    /* reconstruct a full-resolution image (discard interpolated fields whenever possible) */
    /* this has full detail and lowest possible aliasing, but it has high shadow noise and color artifacts when high-iso starts clipping */
#ifndef STDOUT_SILENT
    printf("Full-res reconstruction...\n");
#endif

#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2)
    {
        /* Hoist BRIGHT_ROW out of the inner loop: row-uniform branch. */
        #pragma omp parallel for
        for (int y = 0; y < h; y ++) {
            if (BRIGHT_ROW) {
                fullres_reconstruction_bright_row_avx2(&fullres[(size_t)y*w],
                                                       &bright[(size_t)y*w],
                                                       &dark[(size_t)y*w],
                                                       white_darkened, w);
            } else {
                memcpy(&fullres[(size_t)y*w], &dark[(size_t)y*w], (size_t)w * sizeof(uint32_t));
            }
        }
        return;
    }
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            if (BRIGHT_ROW)
            {
                uint32_t f = bright[x + y*w];
                /* if the brighter copy is overexposed, the guessed pixel for sure has higher brightness */
                fullres[x + y*w] = f < white_darkened ? f : MAX(f, dark[x + y*w]);
            }
            else
            {
                fullres[x + y*w] = dark[x + y*w];
            }
        }
    }
}

static inline int dualiso_raw20_to_ev(const int * raw2ev, uint32_t raw)
{
    return raw2ev[raw > 0xFFFFF ? 0xFFFFF : (int)raw];
}

static inline uint32_t dualiso_ev_weighted_raw(const int * raw2ev,
                                               const int * ev2raw,
                                               uint32_t native,
                                               uint32_t opposite,
                                               int native_weight,
                                               int opposite_weight)
{
    const int denom = native_weight + opposite_weight;
    int ev = (dualiso_raw20_to_ev(raw2ev, native) * native_weight +
              dualiso_raw20_to_ev(raw2ev, opposite) * opposite_weight +
              denom / 2) / denom;
    ev = COERCE(ev, -10 * EV_RESOLUTION, 14 * EV_RESOLUTION - 1);
    return (uint32_t)ev2raw[ev];
}

static inline void fullres_reconstruction_phase_balance(struct raw_info raw_info,
                                                        uint32_t * fullres,
                                                        uint32_t * dark,
                                                        uint32_t * bright,
                                                        uint32_t white_darkened,
                                                        int * is_bright,
                                                        const int * raw2ev,
                                                        const int * ev2raw)
{
    int w = raw_info.width;
    int h = raw_info.height;

#ifndef STDOUT_SILENT
    printf("Full-res phase-balanced reconstruction...\n");
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int bright_row = BRIGHT_ROW;
            const uint32_t native = bright_row ? bright[idx] : dark[idx];
            const uint32_t opposite = bright_row ? dark[idx] : bright[idx];

            if (bright[idx] >= white_darkened)
            {
                fullres[idx] = bright_row ? MAX(native, opposite) : native;
                continue;
            }

            if ((x ^ y) & 1)
            {
                fullres[idx] = dualiso_ev_weighted_raw(raw2ev, ev2raw, native, opposite, 1, 3);
            }
            else
            {
                fullres[idx] = dualiso_ev_weighted_raw(raw2ev, ev2raw, native, opposite, 1, 1);
            }
        }
    }
}

static inline int dualiso_x1_playback_fullres_mesh_fix_enabled(int playback_preview_scale_factor,
                                                               int use_alias_map,
                                                               int use_fullres,
                                                               int chroma_smooth_method)
{
    return playback_preview_scale_factor == 1 &&
           use_fullres &&
           !use_alias_map &&
           chroma_smooth_method;
}

static inline void dualiso_source_mesh_stabilize(struct raw_info raw_info,
                                                 uint32_t * plane,
                                                 uint32_t * source,
                                                 const int * raw2ev,
                                                 const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const size_t plane_pixels = (size_t)w * (size_t)h;
    const size_t plane_bytes = plane_pixels * sizeof(uint32_t);
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int same_color_edge_limit = (EV_RESOLUTION * 3) / 4;
    const int correction_floor = EV_RESOLUTION / 32;

    memcpy(source, plane, plane_bytes);

#ifndef STDOUT_SILENT
    printf("Dual ISO source mesh stabilization...\n");
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int center_ev = dualiso_raw20_to_ev(raw2ev, source[idx]);
            int weighted_ev = center_ev * 2;
            int weight = 2;

            if (y >= 2 && y + 2 < h)
            {
                const size_t up_idx = (size_t)x + (size_t)(y - 2) * (size_t)w;
                const size_t down_idx = (size_t)x + (size_t)(y + 2) * (size_t)w;
                const int up_ev = dualiso_raw20_to_ev(raw2ev, source[up_idx]);
                const int down_ev = dualiso_raw20_to_ev(raw2ev, source[down_idx]);
                if (ABS(up_ev - down_ev) <= same_color_edge_limit)
                {
                    weighted_ev += ((up_ev + down_ev + 1) / 2) * 2;
                    weight += 2;
                }
            }

            if (x >= 2 && x + 2 < w)
            {
                const size_t left_idx = (size_t)(x - 2) + (size_t)y * (size_t)w;
                const size_t right_idx = (size_t)(x + 2) + (size_t)y * (size_t)w;
                const int left_ev = dualiso_raw20_to_ev(raw2ev, source[left_idx]);
                const int right_ev = dualiso_raw20_to_ev(raw2ev, source[right_idx]);
                if (ABS(left_ev - right_ev) <= same_color_edge_limit)
                {
                    weighted_ev += ((left_ev + right_ev + 1) / 2) * 2;
                    weight += 2;
                }
            }

            if (weight == 2)
            {
                continue;
            }

            int corrected_ev = (weighted_ev + weight / 2) / weight;
            if (ABS(center_ev - corrected_ev) < correction_floor)
            {
                continue;
            }
            corrected_ev = COERCE(corrected_ev, ev_min, ev_max);
            plane[idx] = (uint32_t)ev2raw[corrected_ev];
        }
    }
}

static inline void dualiso_output_vertical_mesh_stabilize(struct raw_info raw_info,
                                                          uint32_t * image,
                                                          uint32_t * source,
                                                          const int * raw2ev,
                                                          const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const size_t pixel_count = (size_t)w * (size_t)h;
    const size_t image_bytes = pixel_count * sizeof(uint32_t);
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int same_color_edge_limit = (EV_RESOLUTION * 3) / 4;
    const int correction_floor = EV_RESOLUTION / 32;

    memcpy(source, image, image_bytes);

#ifndef STDOUT_SILENT
    printf("Dual ISO output vertical mesh stabilization...\n");
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 2; y < h - 2; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const size_t up_idx = (size_t)x + (size_t)(y - 2) * (size_t)w;
            const size_t down_idx = (size_t)x + (size_t)(y + 2) * (size_t)w;
            const int up_ev = dualiso_raw20_to_ev(raw2ev, source[up_idx]);
            const int down_ev = dualiso_raw20_to_ev(raw2ev, source[down_idx]);

            if (ABS(up_ev - down_ev) > same_color_edge_limit)
            {
                continue;
            }

            const int center_ev = dualiso_raw20_to_ev(raw2ev, source[idx]);
            const int neighbor_ev = (up_ev + down_ev + 1) / 2;
            if (ABS(center_ev - neighbor_ev) < correction_floor)
            {
                continue;
            }

            int corrected_ev = (center_ev + 2 * neighbor_ev + 1) / 3;
            corrected_ev = COERCE(corrected_ev, ev_min, ev_max);
            image[idx] = (uint32_t)ev2raw[corrected_ev];
        }
    }
}

static inline void dualiso_output_flat_chroma_guard(struct raw_info raw_info,
                                                    uint32_t * image,
                                                    const int * raw2ev,
                                                    const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int green_edge_limit = EV_RESOLUTION * 3 / 2;
    const int correction_floor = EV_RESOLUTION / 32;
    const int max_chroma_correction = EV_RESOLUTION * 2;
    const int red_green_limit = 0;
    const int blue_green_limit = -EV_RESOLUTION / 4;

#ifndef STDOUT_SILENT
    printf("Dual ISO output flat chroma guard...\n");
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 1; y < h - 1; y ++)
    {
        for (int x = 1; x < w - 1; x ++)
        {
            const int red_site = ((x & 1) == 0) && ((y & 1) == 0);
            const int blue_site = ((x & 1) == 1) && ((y & 1) == 1);
            if (!red_site && !blue_site)
            {
                continue;
            }

            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int left_green_ev = dualiso_raw20_to_ev(raw2ev, image[idx - 1]);
            const int right_green_ev = dualiso_raw20_to_ev(raw2ev, image[idx + 1]);
            const int up_green_ev = dualiso_raw20_to_ev(raw2ev, image[idx - (size_t)w]);
            const int down_green_ev = dualiso_raw20_to_ev(raw2ev, image[idx + (size_t)w]);
            const int green_min = MIN(MIN(left_green_ev, right_green_ev),
                                      MIN(up_green_ev, down_green_ev));
            const int green_max = MAX(MAX(left_green_ev, right_green_ev),
                                      MAX(up_green_ev, down_green_ev));
            if (green_max - green_min > green_edge_limit)
            {
                continue;
            }

            const int green_avg = (left_green_ev + right_green_ev + up_green_ev + down_green_ev + 2) / 4;
            const int site_ev = dualiso_raw20_to_ev(raw2ev, image[idx]);
            const int target_ev = green_avg + (red_site ? red_green_limit : blue_green_limit);
            const int excess = site_ev - target_ev;
            if (excess <= correction_floor)
            {
                continue;
            }

            const int correction = MIN(excess, max_chroma_correction);
            const int corrected_ev = COERCE(site_ev - correction, ev_min, ev_max);
            image[idx] = (uint32_t)ev2raw[corrected_ev];
        }
    }
}

static inline void dualiso_output_flat_green_cell_balance(struct raw_info raw_info,
                                                          uint32_t * image,
                                                          const int * raw2ev,
                                                          const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int same_green_edge_limit = EV_RESOLUTION * 3 / 2;
    const int correction_floor = EV_RESOLUTION / 64;
    const int max_green_correction = EV_RESOLUTION / 2;

#ifndef STDOUT_SILENT
    printf("Dual ISO output flat green cell balance...\n");
#endif

    #pragma omp parallel for
    for (int y = 2; y < h - 3; y += 2)
    {
        for (int x = 2; x < w - 3; x += 2)
        {
            const size_t green_red_row_idx = (size_t)(x + 1) + (size_t)y * (size_t)w;
            const size_t green_blue_row_idx = (size_t)x + (size_t)(y + 1) * (size_t)w;

            const int g_red_left_ev = dualiso_raw20_to_ev(raw2ev, image[green_red_row_idx - 2]);
            const int g_red_right_ev = dualiso_raw20_to_ev(raw2ev, image[green_red_row_idx + 2]);
            const int g_red_up_ev = dualiso_raw20_to_ev(raw2ev, image[green_red_row_idx - (size_t)2 * (size_t)w]);
            const int g_red_down_ev = dualiso_raw20_to_ev(raw2ev, image[green_red_row_idx + (size_t)2 * (size_t)w]);
            const int g_blue_left_ev = dualiso_raw20_to_ev(raw2ev, image[green_blue_row_idx - 2]);
            const int g_blue_right_ev = dualiso_raw20_to_ev(raw2ev, image[green_blue_row_idx + 2]);
            const int g_blue_up_ev = dualiso_raw20_to_ev(raw2ev, image[green_blue_row_idx - (size_t)2 * (size_t)w]);
            const int g_blue_down_ev = dualiso_raw20_to_ev(raw2ev, image[green_blue_row_idx + (size_t)2 * (size_t)w]);
            const int red_green_axis_ev = MAX(ABS(g_red_left_ev - g_red_right_ev),
                                              ABS(g_red_up_ev - g_red_down_ev));
            const int blue_green_axis_ev = MAX(ABS(g_blue_left_ev - g_blue_right_ev),
                                               ABS(g_blue_up_ev - g_blue_down_ev));
            if (MAX(red_green_axis_ev, blue_green_axis_ev) > same_green_edge_limit)
            {
                continue;
            }

            const int g_red_ev = dualiso_raw20_to_ev(raw2ev, image[green_red_row_idx]);
            const int g_blue_ev = dualiso_raw20_to_ev(raw2ev, image[green_blue_row_idx]);
            const int delta = g_red_ev - g_blue_ev;
            if (ABS(delta) <= correction_floor)
            {
                continue;
            }

            int correction = delta / 2;
            correction = COERCE(correction, -max_green_correction, max_green_correction);
            int g_red_out_ev = COERCE(g_red_ev - correction, ev_min, ev_max);
            int g_blue_out_ev = COERCE(g_blue_ev + correction, ev_min, ev_max);
            image[green_red_row_idx] = (uint32_t)ev2raw[g_red_out_ev];
            image[green_blue_row_idx] = (uint32_t)ev2raw[g_blue_out_ev];
        }
    }
}

static inline int dualiso_output_dc_mesh_balance(struct raw_info raw_info,
                                                 uint32_t * image,
                                                 const int * raw2ev,
                                                 const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int edge_limit = EV_RESOLUTION * 2;
    const int max_row_correction = (EV_RESOLUTION * 3) / 4;
    const int max_col_correction = EV_RESOLUTION / 2;
    int * row_offset = (int *)calloc((size_t)h, sizeof(int));
    int * col_offset = (int *)calloc((size_t)w, sizeof(int));

    if (!row_offset || !col_offset)
    {
        free(row_offset);
        free(col_offset);
        return 0;
    }

#ifndef STDOUT_SILENT
    printf("Dual ISO output DC mesh balance...\n");
#endif

    #pragma omp parallel for
    for (int y = 2; y < h - 2; y ++)
    {
        long long sum = 0;
        int count = 0;
        for (int x = (y & 1) ? 0 : 1; x < w; x += 2)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int center_ev = dualiso_raw20_to_ev(raw2ev, image[idx]);
            const int up_ev = dualiso_raw20_to_ev(raw2ev, image[(size_t)x + (size_t)(y - 2) * (size_t)w]);
            const int down_ev = dualiso_raw20_to_ev(raw2ev, image[(size_t)x + (size_t)(y + 2) * (size_t)w]);
            if (ABS(up_ev - down_ev) <= edge_limit)
            {
                sum += center_ev - ((up_ev + down_ev + 1) / 2);
                count ++;
            }
        }
        if (count > w / 8)
        {
            row_offset[y] = COERCE((int)(sum / count), -max_row_correction, max_row_correction);
        }
    }

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const int correction = (row_offset[y] * 3) / 4;
            if (correction)
            {
                const size_t idx = (size_t)x + (size_t)y * (size_t)w;
                int ev = dualiso_raw20_to_ev(raw2ev, image[idx]) - correction;
                ev = COERCE(ev, ev_min, ev_max);
                image[idx] = (uint32_t)ev2raw[ev];
            }
        }
    }

    #pragma omp parallel for
    for (int x = 2; x < w - 2; x ++)
    {
        long long sum = 0;
        int count = 0;
        for (int y = (x & 1) ? 0 : 1; y < h; y += 2)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int center_ev = dualiso_raw20_to_ev(raw2ev, image[idx]);
            const int left_ev = dualiso_raw20_to_ev(raw2ev, image[(size_t)(x - 2) + (size_t)y * (size_t)w]);
            const int right_ev = dualiso_raw20_to_ev(raw2ev, image[(size_t)(x + 2) + (size_t)y * (size_t)w]);
            if (ABS(left_ev - right_ev) <= edge_limit)
            {
                sum += center_ev - ((left_ev + right_ev + 1) / 2);
                count ++;
            }
        }
        if (count > h / 8)
        {
            col_offset[x] = COERCE((int)(sum / count), -max_col_correction, max_col_correction);
        }
    }

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const int correction = col_offset[x] / 2;
            if (correction)
            {
                const size_t idx = (size_t)x + (size_t)y * (size_t)w;
                int ev = dualiso_raw20_to_ev(raw2ev, image[idx]) - correction;
                ev = COERCE(ev, ev_min, ev_max);
                image[idx] = (uint32_t)ev2raw[ev];
            }
        }
    }

    free(row_offset);
    free(col_offset);
    return 1;
}

static inline uint16_t dualiso_raw20_to_u16(uint32_t raw)
{
    raw = raw > 0xFFFFF ? 0xFFFFF : raw;
    return (uint16_t)((raw + 8u) >> 4);
}

static inline void dualiso_copy_u16_to_raw20(uint16_t * image_data,
                                             uint32_t * source,
                                             size_t pixel_count)
{
    #pragma omp parallel for
    for (int i = 0; i < (int)pixel_count; i ++)
    {
        source[i] = ((uint32_t)image_data[i]) << 4;
    }
}

static inline void dualiso_output_mesh_axis_raw20(struct raw_info raw_info,
                                                  const uint32_t * source,
                                                  uint32_t * output,
                                                  const int * raw2ev,
                                                  const int * ev2raw,
                                                  int horizontal)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    const int edge_limit = EV_RESOLUTION * 2;
    const int correction_floor = EV_RESOLUTION / 64;

    memcpy(output, source, (size_t)w * (size_t)h * sizeof(uint32_t));

    if (horizontal)
    {
        const int x_end = w - 1;
        #pragma omp parallel for collapse(2)
        for (int y = 0; y < h; y ++)
        {
            for (int x = 1; x < x_end; x ++)
            {
                const size_t idx = (size_t)x + (size_t)y * (size_t)w;
                const int a_ev = dualiso_raw20_to_ev(raw2ev, source[idx - 1]);
                const int b_ev = dualiso_raw20_to_ev(raw2ev, source[idx + 1]);
                if (ABS(a_ev - b_ev) > edge_limit)
                {
                    continue;
                }
                const int center_ev = dualiso_raw20_to_ev(raw2ev, source[idx]);
                const int neighbor_ev = (a_ev + b_ev + 1) / 2;
                if (ABS(center_ev - neighbor_ev) < correction_floor)
                {
                    continue;
                }
                int corrected_ev = (center_ev + neighbor_ev + 1) / 2;
                corrected_ev = COERCE(corrected_ev, ev_min, ev_max);
                output[idx] = (uint32_t)ev2raw[corrected_ev];
            }
        }
    }
    else
    {
        const int y_end = h - 1;
        #pragma omp parallel for collapse(2)
        for (int y = 1; y < y_end; y ++)
        {
            for (int x = 0; x < w; x ++)
            {
                const size_t idx = (size_t)x + (size_t)y * (size_t)w;
                const int a_ev = dualiso_raw20_to_ev(raw2ev, source[idx - (size_t)w]);
                const int b_ev = dualiso_raw20_to_ev(raw2ev, source[idx + (size_t)w]);
                if (ABS(a_ev - b_ev) > edge_limit)
                {
                    continue;
                }
                const int center_ev = dualiso_raw20_to_ev(raw2ev, source[idx]);
                const int neighbor_ev = (a_ev + b_ev + 1) / 2;
                if (ABS(center_ev - neighbor_ev) < correction_floor)
                {
                    continue;
                }
                int corrected_ev = (center_ev + neighbor_ev + 1) / 2;
                corrected_ev = COERCE(corrected_ev, ev_min, ev_max);
                output[idx] = (uint32_t)ev2raw[corrected_ev];
            }
        }
    }
}

static inline void dualiso_output_phase_preserving_mesh_stabilize(struct raw_info raw_info,
                                                                  uint16_t * image_data,
                                                                  uint32_t * source,
                                                                  uint32_t * temp,
                                                                  const int * raw2ev,
                                                                  const int * ev2raw)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const size_t pixel_count = (size_t)w * (size_t)h;
    const int ev_min = -10 * EV_RESOLUTION;
    const int ev_max = 14 * EV_RESOLUTION - 1;
    double source_sum[4] = {0.0, 0.0, 0.0, 0.0};
    double output_sum[4] = {0.0, 0.0, 0.0, 0.0};
    int count[4] = {0, 0, 0, 0};
    int phase_delta[4] = {0, 0, 0, 0};

#ifndef STDOUT_SILENT
    printf("Full-res phase-preserving output mesh stabilization...\n");
#endif

    dualiso_copy_u16_to_raw20(image_data, source, pixel_count);
    dualiso_output_mesh_axis_raw20(raw_info, source, temp, raw2ev, ev2raw, 1);
    dualiso_output_mesh_axis_raw20(raw_info, temp, source, raw2ev, ev2raw, 0);

    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int phase = ((y & 1) << 1) | (x & 1);
            source_sum[phase] += dualiso_raw20_to_ev(raw2ev, ((uint32_t)image_data[idx]) << 4);
            output_sum[phase] += dualiso_raw20_to_ev(raw2ev, source[idx]);
            count[phase] ++;
        }
    }

    for (int phase = 0; phase < 4; phase ++)
    {
        if (count[phase] > 0)
        {
            const double delta = (source_sum[phase] - output_sum[phase]) / (double)count[phase];
            phase_delta[phase] = delta >= 0.0 ? (int)(delta + 0.5) : (int)(delta - 0.5);
        }
    }

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y ++)
    {
        for (int x = 0; x < w; x ++)
        {
            const size_t idx = (size_t)x + (size_t)y * (size_t)w;
            const int phase = ((y & 1) << 1) | (x & 1);
            int ev = dualiso_raw20_to_ev(raw2ev, source[idx]) + phase_delta[phase];
            ev = COERCE(ev, ev_min, ev_max);
            image_data[idx] = dualiso_raw20_to_u16((uint32_t)ev2raw[ev]);
        }
    }
}

static inline void build_alias_map(struct raw_info raw_info,
                                   uint16_t* alias_map,
                                   uint32_t* fullres_smooth,
                                   uint32_t* halfres_smooth,
                                   uint32_t* bright,
                                   int dark_noise,
                                   int black,
                                   int * raw2ev,
                                   dualiso_full20bit_scratch_t * scratch)
{
    if(!alias_map) return;
    
    int w = raw_info.width;
    int h = raw_info.height;
    
    double alias_stage_start = mlv_stage_timing_now();
    double * fullres_curve = build_fullres_curve(scratch, black);
#ifndef STDOUT_SILENT
    printf("Building alias map...\n");
#endif
    uint16_t* alias_aux = ensure_alias_aux_scratch(scratch, (size_t)w * (size_t)h);
    g_dualiso_full20bit_timing.mix_alias_map_setup_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);
    if (!fullres_curve || !alias_aux)
    {
        return;
    }
    
    /* build the aliasing maps (where it's likely to get aliasing) */
    /* do this by comparing fullres and halfres images */
    /* if the difference is small, we'll prefer halfres for less noise, otherwise fullres for less aliasing */
    alias_stage_start = mlv_stage_timing_now();
#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_alias_dispatch_once, dualiso_alias_dispatch_init);
    if (g_dualiso_alias_use_avx2)
    {
        #pragma omp parallel for
        for (int y = 0; y < h; y ++)
        {
            build_alias_map_init_diff_row_avx2(&alias_map[(size_t)y * (size_t)w],
                                                &fullres_smooth[(size_t)y * (size_t)w],
                                                &halfres_smooth[(size_t)y * (size_t)w],
                                                &bright[(size_t)y * (size_t)w],
                                                raw2ev,
                                                fullres_curve,
                                                fullres_thr,
                                                dark_noise,
                                                w);
        }
    }
    else
#endif
    {
        #pragma omp parallel for collapse(2)
        for (int y = 0; y < h; y ++)
        {
            for (int x = 0; x < w; x ++)
            {
                /* do not compute alias map where we'll use fullres detail anyway */
                if (fullres_curve[bright[x + y*w]] > fullres_thr)
                    continue;

                int f = fullres_smooth[x + y*w];
                int h = halfres_smooth[x + y*w];
                int fe = raw2ev[f];
                int he = raw2ev[h];
                int e_lin = ABS(f - h); /* error in linear space, for shadows (downweights noise) */
                e_lin = MAX(e_lin - dark_noise*3/2, 0);
                int e_log = ABS(fe - he); /* error in EV space, for highlights (highly sensitive to noise) */
                alias_map[x + y*w] = MIN(MIN(e_lin/2, e_log/16), 65530);
            }
        }
    }
    g_dualiso_full20bit_timing.mix_alias_map_init_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);

    alias_stage_start = mlv_stage_timing_now();
    memcpy(alias_aux, alias_map, (size_t)w * (size_t)h * sizeof(uint16_t));
    g_dualiso_full20bit_timing.mix_alias_map_copy_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);
#ifndef STDOUT_SILENT
    printf("Filtering alias map...\n");
#endif
    alias_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for collapse(2)
    for (int y = 6; y < h-6; y ++)
    {
        for (int x = 6; x < w-6; x ++)
        {
            /* do not compute alias map where we'll use fullres detail anyway */
            if (fullres_curve[bright[x + y*w]] > fullres_thr)
                continue;
            
            /* Use the 5th max (out of 37) to filter isolated pixels. */
            alias_aux[x + y * w] = alias_map_fifth_largest_37_at(alias_map, w, x, y);
        }
    }
    g_dualiso_full20bit_timing.mix_alias_map_filter_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);
#ifndef STDOUT_SILENT
    printf("Smoothing alias map...\n");
#endif
    /* gaussian blur */
    alias_stage_start = mlv_stage_timing_now();
#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_alias_dispatch_once, dualiso_alias_dispatch_init);
    if (g_dualiso_alias_use_avx2)
    {
        #pragma omp parallel for
        for (int y = 6; y < h-6; y ++)
        {
            build_alias_map_gaussian_row_avx2(&alias_map[(size_t)y * (size_t)w],
                                               alias_aux,
                                               y, w,
                                               bright, fullres_curve, fullres_thr);
        }
    }
    else
#endif
    {
        #pragma omp parallel for collapse(2)
        for (int y = 6; y < h-6; y ++)
        {
            for (int x = 6; x < w-6; x ++)
            {
                /* do not compute alias map where we'll use fullres detail anyway */
                if (fullres_curve[bright[x + y*w]] > fullres_thr)
                    continue;

                int c =
                (alias_aux[x+0 + (y+0) * w])+
                (alias_aux[x+0 + (y-2) * w] + alias_aux[x-2 + (y+0) * w] + alias_aux[x+2 + (y+0) * w] + alias_aux[x+0 + (y+2) * w]) * 820 / 1024 +
                (alias_aux[x-2 + (y-2) * w] + alias_aux[x+2 + (y-2) * w] + alias_aux[x-2 + (y+2) * w] + alias_aux[x+2 + (y+2) * w]) * 657 / 1024 +
                (alias_aux[x+0 + (y-2) * w] + alias_aux[x-2 + (y+0) * w] + alias_aux[x+2 + (y+0) * w] + alias_aux[x+0 + (y+2) * w]) * 421 / 1024 +
                (alias_aux[x-2 + (y-2) * w] + alias_aux[x+2 + (y-2) * w] + alias_aux[x-2 + (y-2) * w] + alias_aux[x+2 + (y-2) * w] + alias_aux[x-2 + (y+2) * w] + alias_aux[x+2 + (y+2) * w] + alias_aux[x-2 + (y+2) * w] + alias_aux[x+2 + (y+2) * w]) * 337 / 1024 +
                (alias_aux[x-2 + (y-2) * w] + alias_aux[x+2 + (y-2) * w] + alias_aux[x-2 + (y+2) * w] + alias_aux[x+2 + (y+2) * w]) * 173 / 1024 +
                (alias_aux[x+0 + (y-6) * w] + alias_aux[x-6 + (y+0) * w] + alias_aux[x+6 + (y+0) * w] + alias_aux[x+0 + (y+6) * w]) * 139 / 1024 +
                (alias_aux[x-2 + (y-6) * w] + alias_aux[x+2 + (y-6) * w] + alias_aux[x-6 + (y-2) * w] + alias_aux[x+6 + (y-2) * w] + alias_aux[x-6 + (y+2) * w] + alias_aux[x+6 + (y+2) * w] + alias_aux[x-2 + (y+6) * w] + alias_aux[x+2 + (y+6) * w]) * 111 / 1024 +
                (alias_aux[x-2 + (y-6) * w] + alias_aux[x+2 + (y-6) * w] + alias_aux[x-6 + (y-2) * w] + alias_aux[x+6 + (y-2) * w] + alias_aux[x-6 + (y+2) * w] + alias_aux[x+6 + (y+2) * w] + alias_aux[x-2 + (y+6) * w] + alias_aux[x+2 + (y+6) * w]) * 57 / 1024;
                alias_map[x + y * w] = c;
            }
        }
    }
    g_dualiso_full20bit_timing.mix_alias_map_gaussian_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);
    
    /* make it grayscale */
    alias_stage_start = mlv_stage_timing_now();
    #pragma omp parallel for collapse(2)
    for (int y = 2; y < h-2; y += 2)
    {
        for (int x = 2; x < w-2; x += 2)
        {
            int a = alias_map[x   +     y * w];
            int b = alias_map[x+1 +     y * w];
            int c = alias_map[x   + (y+1) * w];
            int d = alias_map[x+1 + (y+1) * w];
            int C = MAX(MAX(a,b), MAX(c,d));
            
            C = MIN(C, ALIAS_MAP_MAX);
            
            alias_map[x   +     y * w] =
            alias_map[x+1 +     y * w] =
            alias_map[x   + (y+1) * w] =
            alias_map[x+1 + (y+1) * w] = C;
        }
    }
    g_dualiso_full20bit_timing.mix_alias_map_grayscale_ms +=
        dualiso_debug_elapsed_ms(alias_stage_start);
    
}

#define CHROMA_SMOOTH_TYPE uint32_t

#define CHROMA_SMOOTH_2X2
#include "chroma_smooth.c"
#undef CHROMA_SMOOTH_2X2

#define CHROMA_SMOOTH_3X3
#include "chroma_smooth.c"
#undef CHROMA_SMOOTH_3X3

#define CHROMA_SMOOTH_5X5
#include "chroma_smooth.c"
#undef CHROMA_SMOOTH_5X5

static inline void hdr_chroma_smooth(struct raw_info raw_info, uint32_t * input, uint32_t * output, int method, int * raw2ev, int * ev2raw)
{
    int w = raw_info.width;
    int h = raw_info.height;
    int black = raw_info.black_level;
    int white = raw_info.white_level;
    int kernel_size;

    /* llrawproc.h stores chroma smooth as 0=off, 1=2x2, 2=3x3, 3=5x5. */
    switch (method) {
        case 1:
            kernel_size = 2;
            break;
        case 2:
            kernel_size = 3;
            break;
        case 3:
            kernel_size = 5;
            break;
        default:
            kernel_size = 0;
            break;
    }

    switch (kernel_size) {
        case 2:
            chroma_smooth_2x2(w, h, input, output, raw2ev, ev2raw, black, white);
            break;
        case 3:
            chroma_smooth_3x3(w, h, input, output, raw2ev, ev2raw, black, white);
            break;
        case 5:
            chroma_smooth_5x5(w, h, input, output, raw2ev, ev2raw, black, white);
            break;
            
        default:
#ifndef STDOUT_SILENT
            err_printf("Unsupported chroma smooth method\n");
#endif
            break;
    }
}

static inline int dualiso_supported_chroma_smooth_method(int method)
{
    switch (method) {
        case 1:
        case 2:
        case 3:
            return method;
        default:
            return 0;
    }
}

static void dualiso_debug_dump_source_smooth_plane(const char * name,
                                                   const uint32_t * data,
                                                   size_t bytes)
{
    const char * dir = getenv("DUALISO_DEBUG_SOURCE_SMOOTH_DIR");
    if (!dir || !*dir || !name || !*name || !data || bytes == 0)
    {
        return;
    }

    char path[1200];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE * f = fopen(path, "wb");
    if (!f)
    {
        return;
    }
    fwrite(data, 1, bytes, f);
    fclose(f);
}

static inline int smooth_matched_source_planes(struct raw_info raw_info,
                                               uint32_t * dark,
                                               uint32_t * bright,
                                               uint32_t * dark_smooth,
                                               uint32_t * bright_smooth,
                                               int chroma_smooth_method,
                                               int black,
                                               int dark_white,
                                               int bright_white,
                                               dualiso_full20bit_scratch_t * scratch)
{
    if (!chroma_smooth_method)
    {
        return 1;
    }
    if (!dark || !bright || !dark_smooth || !bright_smooth || !scratch)
    {
        return 0;
    }
    g_dualiso_full20bit_timing.mix_chroma_probe_mode = dualiso_mix_chroma_probe_mode();

    const size_t plane_pixels = (size_t)raw_info.width * (size_t)raw_info.height;
    const size_t plane_bytes = plane_pixels * sizeof(uint32_t);

    double stage_start = mlv_stage_timing_now();
    memcpy(bright_smooth, bright, plane_bytes);
    memcpy(dark_smooth, dark, plane_bytes);
    g_dualiso_full20bit_timing.mix_chroma_copy_ms += dualiso_debug_elapsed_ms(stage_start);

#ifndef STDOUT_SILENT
    printf("Chroma smoothing matched source planes...\n");
#endif
    const double chroma_start = mlv_stage_timing_now();
    int * raw2ev = NULL;
    float * raw2ev_float = NULL;
    int * ev2raw = NULL;
    const double lut_start = mlv_stage_timing_now();
    if (!ensure_scratch_ev_lut(scratch, black, dark_white, &raw2ev, &raw2ev_float, &ev2raw))
    {
        g_dualiso_full20bit_timing.mix_ev_lut_ms += dualiso_debug_elapsed_ms(lut_start);
        return 0;
    }
    (void)raw2ev_float;
    g_dualiso_full20bit_timing.mix_ev_lut_ms += dualiso_debug_elapsed_ms(lut_start);

    stage_start = mlv_stage_timing_now();
    g_dualiso_mix_chroma_probe_stage_cache = 1;
    g_dualiso_full20bit_timing.mix_chroma_probe_stage = 1;
    struct raw_info bright_info = raw_info;
    bright_info.black_level = black;
    bright_info.white_level = bright_white;
    hdr_chroma_smooth(bright_info, bright, bright_smooth, chroma_smooth_method, raw2ev, ev2raw);
    g_dualiso_mix_chroma_probe_stage_cache = 0;
    g_dualiso_full20bit_timing.mix_chroma_probe_stage = 0;
    g_dualiso_full20bit_timing.mix_chroma_fullres_ms += dualiso_debug_elapsed_ms(stage_start);

    stage_start = mlv_stage_timing_now();
    g_dualiso_mix_chroma_probe_stage_cache = 2;
    g_dualiso_full20bit_timing.mix_chroma_probe_stage = 2;
    struct raw_info dark_info = raw_info;
    dark_info.black_level = black;
    dark_info.white_level = dark_white;
    hdr_chroma_smooth(dark_info, dark, dark_smooth, chroma_smooth_method, raw2ev, ev2raw);
    g_dualiso_mix_chroma_probe_stage_cache = 0;
    g_dualiso_full20bit_timing.mix_chroma_probe_stage = 0;
    g_dualiso_full20bit_timing.mix_chroma_halfres_ms += dualiso_debug_elapsed_ms(stage_start);

    dualiso_debug_dump_source_smooth_plane("stage_bright_source_smooth.u32",
                                           bright_smooth,
                                           plane_bytes);
    dualiso_debug_dump_source_smooth_plane("stage_dark_source_smooth.u32",
                                           dark_smooth,
                                           plane_bytes);

    g_dualiso_full20bit_timing.mix_chroma_ms += dualiso_debug_elapsed_ms(chroma_start);
    return 1;
}

static inline int mix_curve_key_matches(const dualiso_full20bit_scratch_t * scratch,
                                        int slot,
                                        size_t required_count,
                                        uint32_t black,
                                        uint32_t white,
                                        double corr_ev,
                                        double overlap)
{
    return scratch->mix_curve_valid[slot]
        && scratch->mix_curve_capacity[slot] >= required_count
        && scratch->mix_curve_last_black[slot] == black
        && scratch->mix_curve_last_white[slot] == white
        && scratch->mix_curve_last_corr_ev[slot] == corr_ev
        && scratch->mix_curve_last_overlap[slot] == overlap;
}

static inline int global_mix_curve_key_matches(const dualiso_mix_curve_cache_entry_t * entry,
                                               size_t required_count,
                                               uint32_t black,
                                               uint32_t white,
                                               double corr_ev,
                                               double overlap)
{
    return entry->valid
        && entry->curve
        && entry->capacity >= required_count
        && entry->black == black
        && entry->white == white
        && entry->corr_ev == corr_ev
        && entry->overlap == overlap;
}

static uint64_t next_global_mix_curve_use_locked(void)
{
    g_mix_curve_cache_use_counter++;
    if (!g_mix_curve_cache_use_counter)
    {
        g_mix_curve_cache_use_counter = 1;
    }
    return g_mix_curve_cache_use_counter;
}

static int copy_mix_curve_from_global_cache(double * mix_curve,
                                            size_t required_count,
                                            uint32_t black,
                                            uint32_t white,
                                            double corr_ev,
                                            double overlap)
{
    int copied = 0;
    if (!mix_curve)
    {
        return 0;
    }

    pthread_mutex_lock(&g_mix_curve_cache_mutex);
    const uint64_t use_counter = next_global_mix_curve_use_locked();
    for (int slot = 0; slot < DUALISO_GLOBAL_MIX_CURVE_CACHE_SLOTS; slot++)
    {
        dualiso_mix_curve_cache_entry_t * entry = &g_mix_curve_cache[slot];
        if (global_mix_curve_key_matches(entry, required_count, black, white, corr_ev, overlap))
        {
            memcpy(mix_curve, entry->curve, required_count * sizeof(*mix_curve));
            entry->last_used = use_counter;
            copied = 1;
            break;
        }
    }
    pthread_mutex_unlock(&g_mix_curve_cache_mutex);
    return copied;
}

static void publish_mix_curve_to_global_cache(const double * mix_curve,
                                              size_t required_count,
                                              uint32_t black,
                                              uint32_t white,
                                              double corr_ev,
                                              double overlap)
{
    if (!mix_curve)
    {
        return;
    }

    pthread_mutex_lock(&g_mix_curve_cache_mutex);
    const uint64_t use_counter = next_global_mix_curve_use_locked();
    int selected_slot = -1;
    uint64_t oldest_use = UINT64_MAX;
    for (int slot = 0; slot < DUALISO_GLOBAL_MIX_CURVE_CACHE_SLOTS; slot++)
    {
        dualiso_mix_curve_cache_entry_t * entry = &g_mix_curve_cache[slot];
        if (global_mix_curve_key_matches(entry, required_count, black, white, corr_ev, overlap))
        {
            selected_slot = slot;
            break;
        }
        if (!entry->valid)
        {
            selected_slot = slot;
            break;
        }
        if (entry->last_used < oldest_use)
        {
            oldest_use = entry->last_used;
            selected_slot = slot;
        }
    }

    if (selected_slot >= 0)
    {
        dualiso_mix_curve_cache_entry_t * entry = &g_mix_curve_cache[selected_slot];
        if (entry->capacity < required_count)
        {
            double * resized = realloc(entry->curve, required_count * sizeof(*entry->curve));
            if (!resized)
            {
                pthread_mutex_unlock(&g_mix_curve_cache_mutex);
                return;
            }
            entry->curve = resized;
            entry->capacity = required_count;
        }

        entry->valid = 0;
        memcpy(entry->curve, mix_curve, required_count * sizeof(*mix_curve));
        entry->black = black;
        entry->white = white;
        entry->corr_ev = corr_ev;
        entry->overlap = overlap;
        entry->last_used = use_counter;
        entry->valid = 1;
    }
    pthread_mutex_unlock(&g_mix_curve_cache_mutex);
}

static inline double * select_mix_curve_cache_slot(dualiso_full20bit_scratch_t * scratch,
                                                   size_t required_count,
                                                   uint32_t black,
                                                   uint32_t white,
                                                   double corr_ev,
                                                   double overlap,
                                                   int * selected_slot_out,
                                                   int * needs_rebuild,
                                                   int * global_hit)
{
    if (!scratch || !selected_slot_out || !needs_rebuild || !global_hit)
    {
        return NULL;
    }
    *global_hit = 0;

    scratch->mix_curve_use_counter++;
    if (!scratch->mix_curve_use_counter)
    {
        scratch->mix_curve_use_counter = 1;
    }

    for (int slot = 0; slot < DUALISO_MIX_CURVE_CACHE_SLOTS; slot++)
    {
        if (mix_curve_key_matches(scratch, slot, required_count, black, white, corr_ev, overlap))
        {
            scratch->mix_curve_last_used[slot] = scratch->mix_curve_use_counter;
            *selected_slot_out = slot;
            *needs_rebuild = 0;
            return scratch->mix_curve[slot];
        }
    }

    int selected_slot = 0;
    uint64_t oldest_use = UINT64_MAX;
    for (int slot = 0; slot < DUALISO_MIX_CURVE_CACHE_SLOTS; slot++)
    {
        if (!scratch->mix_curve_valid[slot])
        {
            selected_slot = slot;
            break;
        }
        if (scratch->mix_curve_last_used[slot] < oldest_use)
        {
            oldest_use = scratch->mix_curve_last_used[slot];
            selected_slot = slot;
        }
    }

    double * mix_curve = ensure_double_scratch_buffer(&scratch->mix_curve[selected_slot],
                                                      &scratch->mix_curve_capacity[selected_slot],
                                                      required_count);
    if (!mix_curve)
    {
        return NULL;
    }

    scratch->mix_curve_valid[selected_slot] = 0;
    scratch->mix_curve_float_valid[selected_slot] = 0;
    scratch->mix_curve_last_used[selected_slot] = scratch->mix_curve_use_counter;
    scratch->mix_curve_last_black[selected_slot] = black;
    scratch->mix_curve_last_white[selected_slot] = white;
    scratch->mix_curve_last_corr_ev[selected_slot] = corr_ev;
    scratch->mix_curve_last_overlap[selected_slot] = overlap;
    *selected_slot_out = selected_slot;
    if (copy_mix_curve_from_global_cache(mix_curve, required_count, black, white, corr_ev, overlap))
    {
        scratch->mix_curve_valid[selected_slot] = 1;
        *needs_rebuild = 0;
        *global_hit = 1;
    }
    else
    {
        *needs_rebuild = 1;
    }
    return mix_curve;
}

static int prepare_mix_curve_and_ev_lut(uint32_t black,
                                        uint32_t white,
                                        double corr_ev,
                                        double lowiso_dr,
                                        dualiso_full20bit_scratch_t * scratch,
                                        int emit_messages,
                                        double ** mix_curve_out,
                                        int * mix_curve_slot_out,
                                        int ** raw2ev_out,
                                        float ** raw2ev_float_out,
                                        int ** ev2raw_out)
{
    if (!scratch || !mix_curve_out || !mix_curve_slot_out || !raw2ev_out || !raw2ev_float_out || !ev2raw_out
        || white <= black || white >= (1u << 20)
        || white - black >= 16383u * 64u
        || !isfinite(corr_ev) || !isfinite(lowiso_dr))
    {
        return 0;
    }

    *mix_curve_out = NULL;
    *mix_curve_slot_out = 0;
    *raw2ev_out = NULL;
    *raw2ev_float_out = NULL;
    *ev2raw_out = NULL;
    (void)emit_messages;

    double overlap = lowiso_dr - corr_ev;
    overlap = (overlap < 6.0) ? 3.0 : overlap - 3.0;
#ifndef STDOUT_SILENT
    if (emit_messages)
    {
        printf("ISO overlap     : %.1f EV (approx)\n", overlap);
    }
#endif
    if (overlap < 0.5)
    {
#ifndef STDOUT_SILENT
        if (emit_messages)
        {
            printf("Overlap error\n");
        }
#endif
        return 0;
    }
    else if (overlap < 2)
    {
#ifndef STDOUT_SILENT
        if (emit_messages)
        {
            printf("Overlap too small, use a smaller ISO difference for better results.\n");
        }
#endif
    }
#ifndef STDOUT_SILENT
    if (emit_messages)
    {
        printf("Half-res blending...\n");
    }
#endif

    const double max_ev = log2(white / 64 - black / 64);
    const size_t mix_curve_required = (size_t)(1u << 20);
    int mix_curve_slot = 0;
    int mix_curve_needs_rebuild = 0;
    int mix_curve_global_hit = 0;
    double mix_stage_start = mlv_stage_timing_now();
    double * mix_curve = select_mix_curve_cache_slot(scratch,
                                                     mix_curve_required,
                                                     black,
                                                     white,
                                                     corr_ev,
                                                     overlap,
                                                     &mix_curve_slot,
                                                     &mix_curve_needs_rebuild,
                                                     &mix_curve_global_hit);
    g_dualiso_full20bit_timing.mix_curve_select_ms +=
        dualiso_debug_elapsed_ms(mix_stage_start);
    if (!mix_curve)
    {
        return 0;
    }

    g_dualiso_full20bit_timing.mix_curve_corr_ev = corr_ev;
    g_dualiso_full20bit_timing.mix_curve_overlap = overlap;
    g_dualiso_full20bit_timing.mix_curve_rebuilt = mix_curve_needs_rebuild;
    g_dualiso_full20bit_timing.mix_curve_global_hit = mix_curve_global_hit;
    if (mix_curve_needs_rebuild)
    {
        mix_stage_start = mlv_stage_timing_now();
        #pragma omp parallel for
        for (int i = 0; i < 1<<20; i++)
        {
            double ev = log2(MAX(i / 64.0 - black / 64.0, 1)) + corr_ev;
            double c = -cos(MAX(MIN(ev - (max_ev - overlap), overlap), 0) * M_PI / overlap);
            double k = (c + 1) / 2;
            mix_curve[i] = k;
        }
        scratch->mix_curve_valid[mix_curve_slot] = 1;
        publish_mix_curve_to_global_cache(mix_curve, mix_curve_required, black, white, corr_ev, overlap);
        g_dualiso_full20bit_timing.mix_curve_build_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
    }

    int * raw2ev = NULL;
    float * raw2ev_float = NULL;
    int * ev2raw = NULL;
    mix_stage_start = mlv_stage_timing_now();
    if (!ensure_scratch_ev_lut(scratch, black, white, &raw2ev, &raw2ev_float, &ev2raw))
    {
        g_dualiso_full20bit_timing.mix_ev_lut_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
        return 0;
    }
    g_dualiso_full20bit_timing.mix_ev_lut_ms +=
        dualiso_debug_elapsed_ms(mix_stage_start);

    *mix_curve_out = mix_curve;
    *mix_curve_slot_out = mix_curve_slot;
    *raw2ev_out = raw2ev;
    *raw2ev_float_out = raw2ev_float;
    *ev2raw_out = ev2raw;
    return 1;
}

#ifdef DUALISO_AVX2_AVAILABLE
static inline float * ensure_mix_curve_float_cache_slot(dualiso_full20bit_scratch_t * scratch,
                                                        int slot,
                                                        const double * mix_curve,
                                                        size_t required_count)
{
    if (!scratch || slot < 0 || slot >= DUALISO_MIX_CURVE_CACHE_SLOTS || !mix_curve)
    {
        return NULL;
    }

    if (scratch->mix_curve_float_valid[slot]
        && scratch->mix_curve_float_capacity[slot] >= required_count
        && scratch->mix_curve_float[slot])
    {
        return scratch->mix_curve_float[slot];
    }

    float * mix_curve_float = ensure_float_scratch_buffer(&scratch->mix_curve_float[slot],
                                                          &scratch->mix_curve_float_capacity[slot],
                                                          required_count);
    if (!mix_curve_float)
    {
        return NULL;
    }

    #pragma omp parallel for
    for (int i = 0; i < (int)required_count; i++)
    {
        mix_curve_float[i] = (float)mix_curve[i];
    }

    scratch->mix_curve_float_valid[slot] = 1;
    return mix_curve_float;
}
#endif

static inline int mix_images(struct raw_info raw_info,
                             uint32_t * __restrict fullres,
                             uint32_t * __restrict fullres_smooth,
                             uint32_t * __restrict halfres,
                             uint32_t * __restrict halfres_smooth,
                             uint16_t * __restrict alias_map,
                             uint32_t * __restrict dark,
                             uint32_t * __restrict bright,
                             uint16_t * __restrict overexposed,
                             int dark_noise,
                             uint32_t white_darkened,
                             double corr_ev,
                             double lowiso_dr,
                             uint32_t black,
                             uint32_t white,
                             int chroma_smooth_method,
                             dualiso_full20bit_scratch_t * scratch)
{
    int w = raw_info.width;
    int h = raw_info.height;

    /* mix the two images */
    /* highlights:  keep data from dark image only */
    /* shadows:     keep data from bright image only */
    /* midtones:    mix data from both, to bring back the resolution */

    int mix_curve_slot = 0;
    const size_t mix_curve_required = (size_t)(1u << 20);
    double * mix_curve = NULL;
    int * raw2ev = NULL;
    float * raw2ev_float = NULL;
    int * ev2raw = NULL;
    double mix_stage_start = 0.0;
    if (!prepare_mix_curve_and_ev_lut(black,
                                      white,
                                      corr_ev,
                                      lowiso_dr,
                                      scratch,
                                      1,
                                      &mix_curve,
                                      &mix_curve_slot,
                                      &raw2ev,
                                      &raw2ev_float,
                                      &ev2raw))
    {
        return 0;
    }

#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2)
    {
        const int mix_halfres_probe_mode = dualiso_mix_halfres_probe_mode();
        const int mix_halfres_probe_detail = mix_halfres_probe_mode >= 0;
        g_dualiso_full20bit_timing.mix_halfres_probe_mode = mix_halfres_probe_mode;
        const int x_start = w & ~7;
        mix_stage_start = mlv_stage_timing_now();
        float * mix_curve_float = ensure_mix_curve_float_cache_slot(scratch,
                                                                    mix_curve_slot,
                                                                    mix_curve,
                                                                    mix_curve_required);
        g_dualiso_full20bit_timing.mix_curve_float_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
        if (!mix_curve_float)
        {
            return 0;
        }

        mix_stage_start = mlv_stage_timing_now();
        #pragma omp parallel for
        for (int y = 0; y < h; y ++) {
            const size_t row_offset = (size_t)y * (size_t)w;
            uint32_t *halfres_row = &halfres[row_offset];
            const uint32_t *bright_row = &bright[row_offset];
            const uint32_t *dark_row = &dark[row_offset];

            const double row_bulk_start = mix_halfres_probe_detail ? mlv_stage_timing_now() : 0.0;
            mix_images_row_avx2(halfres_row,
                                bright_row,
                                dark_row,
                                raw2ev_float, raw2ev, ev2raw, mix_curve_float, w);
            if (mix_halfres_probe_detail)
            {
                g_dualiso_full20bit_timing.mix_halfres_avx2_bulk_ms +=
                    dualiso_debug_elapsed_ms(row_bulk_start);
            }
            /* tail: pixels not covered by SIMD bulk */
            const double row_tail_start = mix_halfres_probe_detail ? mlv_stage_timing_now() : 0.0;
            for (int x = x_start; x < w; x ++) {
                int b = bright_row[x];
                int d = dark_row[x];
                int bev = raw2ev[b];
                int dev = raw2ev[d];
                double k = COERCE(mix_curve_float[b & 0xFFFFF], 0, 1);
                int mixed = bev * (1-k) + dev * k;
                halfres_row[x] = ev2raw[mixed];
            }
            if (mix_halfres_probe_detail)
            {
                g_dualiso_full20bit_timing.mix_halfres_scalar_tail_ms +=
                    dualiso_debug_elapsed_ms(row_tail_start);
            }
        }
        g_dualiso_full20bit_timing.mix_halfres_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
    }
    else
#endif
    {
        const int mix_halfres_probe_mode = dualiso_mix_halfres_probe_mode();
        const int mix_halfres_probe_detail = mix_halfres_probe_mode >= 0;
        g_dualiso_full20bit_timing.mix_halfres_probe_mode = mix_halfres_probe_mode;
        mix_stage_start = mlv_stage_timing_now();
        const double scalar_tail_start = mix_halfres_probe_detail ? mlv_stage_timing_now() : 0.0;
        #pragma omp parallel for collapse(2)
        for (int y = 0; y < h; y ++)
        {
            for (int x = 0; x < w; x ++)
            {
                /* bright and dark source pixels  */
                /* they may be real or interpolated */
                /* they both have the same brightness (they were adjusted before this loop), so we are ready to mix them */
                int b = bright[x + y*w];
                int d = dark[x + y*w];

                /* go from linear to EV space */
                int bev = raw2ev[b];
                int dev = raw2ev[d];

                /* blending factor */
                double k = COERCE(mix_curve[b & 0xFFFFF], 0, 1);

                /* mix bright and dark exposures */
                int mixed = bev * (1-k) + dev * k;
                halfres[x + y*w] = ev2raw[mixed];
            }
        }
        if (mix_halfres_probe_detail)
        {
            g_dualiso_full20bit_timing.mix_halfres_scalar_tail_ms +=
                dualiso_debug_elapsed_ms(scalar_tail_start);
        }
        g_dualiso_full20bit_timing.mix_halfres_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
    }
    if (chroma_smooth_method)
    {
        g_dualiso_full20bit_timing.mix_chroma_probe_mode = dualiso_mix_chroma_probe_mode();
#ifndef STDOUT_SILENT
        printf("Chroma smoothing...\n");
#endif
        const double chroma_stage_start = mlv_stage_timing_now();
        mix_stage_start = mlv_stage_timing_now();
        if (w > 0 && h > 0)
        {
            const size_t plane_pixels = (size_t)w * (size_t)h;
            const size_t plane_bytes = plane_pixels * sizeof(uint32_t);
            const int copy_fullres = fullres_smooth != fullres;

            if (copy_fullres)
            {
                memcpy(fullres_smooth, fullres, plane_bytes);
            }

            memcpy(halfres_smooth, halfres, plane_bytes);
        }
        g_dualiso_full20bit_timing.mix_chroma_copy_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);

        mix_stage_start = mlv_stage_timing_now();
        g_dualiso_mix_chroma_probe_stage_cache = 1;
        g_dualiso_full20bit_timing.mix_chroma_probe_stage = 1;
        hdr_chroma_smooth(raw_info, fullres, fullres_smooth, chroma_smooth_method, raw2ev, ev2raw);
        g_dualiso_mix_chroma_probe_stage_cache = 0;
        g_dualiso_full20bit_timing.mix_chroma_probe_stage = 0;
        g_dualiso_full20bit_timing.mix_chroma_fullres_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);

        mix_stage_start = mlv_stage_timing_now();
        g_dualiso_mix_chroma_probe_stage_cache = 2;
        g_dualiso_full20bit_timing.mix_chroma_probe_stage = 2;
        hdr_chroma_smooth(raw_info, halfres, halfres_smooth, chroma_smooth_method, raw2ev, ev2raw);
        g_dualiso_mix_chroma_probe_stage_cache = 0;
        g_dualiso_full20bit_timing.mix_chroma_probe_stage = 0;
        g_dualiso_full20bit_timing.mix_chroma_halfres_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
        g_dualiso_full20bit_timing.mix_chroma_ms +=
            dualiso_debug_elapsed_ms(chroma_stage_start);
    }
    if(alias_map)
    {
        mix_stage_start = mlv_stage_timing_now();
        build_alias_map(raw_info, alias_map, fullres_smooth, halfres_smooth, bright, dark_noise, black, raw2ev, scratch);
        g_dualiso_full20bit_timing.mix_alias_map_ms +=
            dualiso_debug_elapsed_ms(mix_stage_start);
    }
    
    const double overexposed_start = mlv_stage_timing_now();
    /* "Blur" reads the unblurred overexposure map from over_aux and writes the
     * interior result to overexposed. Write the mark pass directly to over_aux
     * and copy only the boundary pixels that the blur leaves unchanged. */
    uint16_t* over_aux = scratch ? scratch->over_aux : NULL;
    if (!over_aux)
    {
        return 0;
    }

    int overexposed_avx2 = 0;
#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2)
    {
        overexposed_avx2 = 1;
        #pragma omp parallel for
        for (int y = 0; y < h; y ++)
        {
            overexposed_mark_row_avx2(&over_aux[(size_t)y * w],
                                      &bright[(size_t)y * w],
                                      &dark[(size_t)y * w],
                                      white_darkened,
                                      white,
                                      w);
        }
    }
#endif
    if (!overexposed_avx2)
    {
        #pragma omp parallel for collapse(2)
        for (int y = 0; y < h; y ++)
        {
            for (int x = 0; x < w; x ++)
            {
                over_aux[x + y * w] = bright[x + y * w] >= white_darkened || dark[x + y * w] >= white ? 100 : 0;
            }
        }
    }

    if (w > 6 && h > 6)
    {
        const size_t row_bytes = (size_t)w * sizeof(uint16_t);
        memcpy(overexposed, over_aux, 3 * row_bytes);
        memcpy(&overexposed[(size_t)(h - 3) * w],
               &over_aux[(size_t)(h - 3) * w],
               3 * row_bytes);

        for (int y = 3; y < h - 3; ++y)
        {
            uint16_t * dst_row = &overexposed[(size_t)y * w];
            const uint16_t * src_row = &over_aux[(size_t)y * w];
            dst_row[0] = src_row[0];
            dst_row[1] = src_row[1];
            dst_row[2] = src_row[2];
            dst_row[w - 3] = src_row[w - 3];
            dst_row[w - 2] = src_row[w - 2];
            dst_row[w - 1] = src_row[w - 1];
        }
    }
    else
    {
        memcpy(overexposed, over_aux, (size_t)w * h * sizeof(uint16_t));
    }

#ifdef DUALISO_AVX2_AVAILABLE
    if (overexposed_avx2)
    {
        #pragma omp parallel for
        for (int y = 3; y < h-3; y ++)
        {
            overexposed_blur_row_avx2(&overexposed[(size_t)y * w],
                                      &over_aux[(size_t)(y - 1) * w],
                                      &over_aux[(size_t)y * w],
                                      &over_aux[(size_t)(y + 1) * w],
                                      w);
        }
    }
#endif
    if (!overexposed_avx2)
    {
        #pragma omp parallel for collapse(2)
        for (int y = 3; y < h-3; y ++)
        {
            for (int x = 3; x < w-3; x ++)
            {
                overexposed[x + y * w] =
                (over_aux[x+0 + (y+0) * w])+
                (over_aux[x+0 + (y-1) * w] + over_aux[x-1 + (y+0) * w] + over_aux[x+1 + (y+0) * w] + over_aux[x+0 + (y+1) * w]) * 820 / 1024 +
                (over_aux[x-1 + (y-1) * w] + over_aux[x+1 + (y-1) * w] + over_aux[x-1 + (y+1) * w] + over_aux[x+1 + (y+1) * w]) * 657 / 1024 +
                //~ (over_aux[x+0 + (y-2) * w] + over_aux[x-2 + (y+0) * w] + over_aux[x+2 + (y+0) * w] + over_aux[x+0 + (y+2) * w]) * 421 / 1024 +
                //~ (over_aux[x-1 + (y-2) * w] + over_aux[x+1 + (y-2) * w] + over_aux[x-2 + (y-1) * w] + over_aux[x+2 + (y-1) * w] + over_aux[x-2 + (y+1) * w] + over_aux[x+2 + (y+1) * w] + over_aux[x-1 + (y+2) * w] + over_aux[x+1 + (y+2) * w]) * 337 / 1024 +
                //~ (over_aux[x-2 + (y-2) * w] + over_aux[x+2 + (y-2) * w] + over_aux[x-2 + (y+2) * w] + over_aux[x+2 + (y+2) * w]) * 173 / 1024 +
                //~ (over_aux[x+0 + (y-3) * w] + over_aux[x-3 + (y+0) * w] + over_aux[x+3 + (y+0) * w] + over_aux[x+0 + (y+3) * w]) * 139 / 1024 +
                //~ (over_aux[x-1 + (y-3) * w] + over_aux[x+1 + (y-3) * w] + over_aux[x-3 + (y-1) * w] + over_aux[x+3 + (y-1) * w] + over_aux[x-3 + (y+1) * w] + over_aux[x+3 + (y+1) * w] + over_aux[x-1 + (y+3) * w] + over_aux[x+1 + (y+3) * w]) * 111 / 1024 +
                //~ (over_aux[x-2 + (y-3) * w] + over_aux[x+2 + (y-3) * w] + over_aux[x-3 + (y-2) * w] + over_aux[x+3 + (y-2) * w] + over_aux[x-3 + (y+2) * w] + over_aux[x+3 + (y+2) * w] + over_aux[x-2 + (y+3) * w] + over_aux[x+2 + (y+3) * w]) * 57 / 1024;
                0;
            }
        }
    }
    g_dualiso_full20bit_timing.mix_overexposed_ms += dualiso_debug_elapsed_ms(overexposed_start);

    return 1;
}

static inline int final_blend(struct raw_info raw_info,
                               uint16_t * __restrict image_data,
                               uint32_t * __restrict raw_buffer_32,
                               uint32_t * __restrict fullres,
                               uint32_t * __restrict fullres_smooth,
                               uint32_t * __restrict halfres_smooth,
                               uint32_t * __restrict dark,
                               uint32_t * __restrict bright,
                               uint16_t * __restrict overexposed,
                               uint16_t * __restrict alias_map,
                               int black,
                               int white,
                               int dark_noise,
                               const float * randn_cache,
                               int fullres_mesh_guard,
                               dualiso_full20bit_scratch_t * scratch)
{
    double final_blend_setup_start = mlv_stage_timing_now();
    /* fullres mixing curve */
    const int use_float_fullres_curve = use_final_blend_float_fullres_curve();
    double * fullres_curve = use_float_fullres_curve ? NULL : build_fullres_curve(scratch, black);
    float * fullres_curve_f = use_float_fullres_curve ? build_fullres_curve_float(scratch, black) : NULL;
    
    int w = raw_info.width;
    int h = raw_info.height;
    
    int * raw2ev = NULL;
    float * raw2ev_float = NULL;
    int * ev2raw = NULL;
    const int final_blend_probe_mode = dualiso_final_blend_probe_mode();
    const int final_blend_probe_all = (final_blend_probe_mode == 0);
    const int final_blend_probe_raw2ev = final_blend_probe_all || final_blend_probe_mode == 1;
    const int final_blend_probe_fullres_curve = final_blend_probe_all || final_blend_probe_mode == 2;
    const int final_blend_probe_ev2raw_store = final_blend_probe_all || final_blend_probe_mode == 3;
    const int final_blend_probe_arithmetic = final_blend_probe_all || final_blend_probe_mode == 4;
    const int final_blend_probe_detail = final_blend_probe_mode >= 0;
    g_dualiso_full20bit_timing.final_blend_probe_mode = final_blend_probe_mode;
    (void)final_blend_probe_all;
    (void)final_blend_probe_raw2ev;
    (void)final_blend_probe_fullres_curve;
    (void)final_blend_probe_ev2raw_store;
    (void)final_blend_probe_arithmetic;
    (void)final_blend_probe_detail;
    if((use_float_fullres_curve && !fullres_curve_f) || (!use_float_fullres_curve && !fullres_curve))
    {
        g_dualiso_full20bit_timing.final_blend_setup_ms += dualiso_debug_elapsed_ms(final_blend_setup_start);
        return -1;
    }
    if (!ensure_scratch_ev_lut(scratch, black, white, &raw2ev, &raw2ev_float, &ev2raw))
    {
        g_dualiso_full20bit_timing.final_blend_setup_ms += dualiso_debug_elapsed_ms(final_blend_setup_start);
        return -1;
    }
    g_dualiso_full20bit_timing.final_blend_setup_ms += dualiso_debug_elapsed_ms(final_blend_setup_start);
#ifndef STDOUT_SILENT
    printf("Final blending...\n");
#endif

    double final_blend_row_kernel_start = mlv_stage_timing_now();
    int final_blend_fused_to_16bit = 0;

#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2 && !fullres_mesh_guard)
    {
        const int x_start = w & ~7;
        const int fuse_to_16bit = 1;
        double final_blend_raw2ev_gather_probe_ms = 0.0;
        double final_blend_fullres_curve_gather_probe_ms = 0.0;
        double final_blend_ev2raw_store_probe_ms = 0.0;
        double final_blend_arithmetic_probe_ms = 0.0;
        double final_blend_overexposed_density = 0.0;
        double final_blend_cap_clamp_pct = 0.0;
        double final_blend_f_near_0_pct = 0.0;
        double final_blend_f_near_1_pct = 0.0;
        if (alias_map)
        {
            #pragma omp parallel for reduction(+:final_blend_raw2ev_gather_probe_ms,final_blend_fullres_curve_gather_probe_ms,final_blend_ev2raw_store_probe_ms,final_blend_arithmetic_probe_ms,final_blend_overexposed_density,final_blend_cap_clamp_pct,final_blend_f_near_0_pct,final_blend_f_near_1_pct)
            for (int y = 0; y < h; y ++) {
                const size_t row_offset = (size_t)y * (size_t)w;
                uint32_t *raw_row = &raw_buffer_32[row_offset];
                uint16_t *image_row = &image_data[row_offset];
                int row_k = (y * w) & 1023;
                const uint32_t *fullres_row = &fullres[row_offset];
                const uint32_t *fullres_smooth_row = &fullres_smooth[row_offset];
                const uint32_t *halfres_smooth_row = &halfres_smooth[row_offset];
                const uint32_t *dark_row = &dark[row_offset];
                const uint32_t *bright_row = &bright[row_offset];
                const uint16_t *overexposed_row = &overexposed[row_offset];
                const uint16_t *alias_row = &alias_map[row_offset];
                double row_raw2ev_gather_probe_ms = 0.0;
                double row_fullres_curve_gather_probe_ms = 0.0;
                double row_ev2raw_store_probe_ms = 0.0;
                double row_arithmetic_probe_ms = 0.0;
                double row_overexposed_density = 0.0;
                double row_cap_clamp_pct = 0.0;
                double row_f_near_0_pct = 0.0;
                double row_f_near_1_pct = 0.0;

                final_blend_row_avx2(raw_row,
                                     image_row,
                                     fullres_row,
                                     fullres_smooth_row,
                                     halfres_smooth_row,
                                     dark_row,
                                     bright_row,
                                     overexposed_row,
                                     alias_row,
                                     raw2ev_float, raw2ev, ev2raw, fullres_curve, fullres_curve_f,
                                     black, dark_noise, w,
                                     randn_cache,
                                     &row_k,
                                     fuse_to_16bit,
                                     final_blend_probe_mode,
                                     &row_raw2ev_gather_probe_ms,
                                     &row_fullres_curve_gather_probe_ms,
                                     &row_arithmetic_probe_ms,
                                     &row_ev2raw_store_probe_ms,
                                     &row_overexposed_density,
                                     &row_cap_clamp_pct,
                                     &row_f_near_0_pct,
                                     &row_f_near_1_pct);
                /* tail: pixels not covered by SIMD bulk */
                for (int x = x_start; x < w; x ++) {
                    int b = bright_row[x];
                    int hr = halfres_smooth_row[x];
                    int fr = fullres_row[x];
                    int frs = fullres_smooth_row[x];
                    int hrev = raw2ev[hr];
                    int frev = raw2ev[fr];
                    int frsev = raw2ev[frs];
                    double f = use_float_fullres_curve
                        ? (double)fullres_curve_f[b & 0xFFFFF]
                        : fullres_curve[b & 0xFFFFF];
                    int co = alias_row[x];
                    double c = COERCE(co / (double) ALIAS_MAP_MAX, 0, 1);
                    double ovf = COERCE(overexposed_row[x] / 200.0, 0, 1);
                    c = MAX(c, ovf);
                    double noisy_or_overexposed = MAX(ovf, 1-f);
                    f = MAX(f, c);
                    int sig = (dark_row[x] + bright_row[x]) / 2;
                    double cap_limit = (double)(sig - black) / (4*dark_noise);
                    if (final_blend_probe_detail)
                    {
                        if (ovf > 0.0) row_overexposed_density += 1.0;
                        if (f > cap_limit) row_cap_clamp_pct += 1.0;
                        if (f <= 0.01) row_f_near_0_pct += 1.0;
                        if (f >= 0.99) row_f_near_1_pct += 1.0;
                    }
                    double fev = noisy_or_overexposed * frsev + (1-noisy_or_overexposed) * frev;
                    f = MAX(0, MIN(f, cap_limit));
                    int output = hrev * (1-f) + fev * f;
                    output = COERCE(output, -10*EV_RESOLUTION, 14*EV_RESOLUTION-1);
                    if (fuse_to_16bit)
                    {
                        float dither = randn_cache[row_k & 1023];
                        row_k = (row_k + 1) & 1023;
                        int v = (int)((ev2raw[output] / 16.0f) + dither + 0.5f);
                        image_row[x] = (uint16_t)COERCE(v, 0, 0xFFFF);
                    }
                    else
                    {
                        raw_set_pixel32(x, y, ev2raw[output]);
                    }
                }
                final_blend_raw2ev_gather_probe_ms += row_raw2ev_gather_probe_ms;
                final_blend_fullres_curve_gather_probe_ms += row_fullres_curve_gather_probe_ms;
                final_blend_ev2raw_store_probe_ms += row_ev2raw_store_probe_ms;
                final_blend_arithmetic_probe_ms += row_arithmetic_probe_ms;
                final_blend_overexposed_density += row_overexposed_density;
                final_blend_cap_clamp_pct += row_cap_clamp_pct;
                final_blend_f_near_0_pct += row_f_near_0_pct;
                final_blend_f_near_1_pct += row_f_near_1_pct;
            }
        }
        else
        {
            #pragma omp parallel for reduction(+:final_blend_raw2ev_gather_probe_ms,final_blend_fullres_curve_gather_probe_ms,final_blend_ev2raw_store_probe_ms,final_blend_arithmetic_probe_ms,final_blend_overexposed_density,final_blend_cap_clamp_pct,final_blend_f_near_0_pct,final_blend_f_near_1_pct)
            for (int y = 0; y < h; y ++) {
                const size_t row_offset = (size_t)y * (size_t)w;
                uint32_t *raw_row = &raw_buffer_32[row_offset];
                uint16_t *image_row = &image_data[row_offset];
                int row_k = (y * w) & 1023;
                const uint32_t *fullres_row = &fullres[row_offset];
                const uint32_t *fullres_smooth_row = &fullres_smooth[row_offset];
                const uint32_t *halfres_smooth_row = &halfres_smooth[row_offset];
                const uint32_t *dark_row = &dark[row_offset];
                const uint32_t *bright_row = &bright[row_offset];
                const uint16_t *overexposed_row = &overexposed[row_offset];
                double row_raw2ev_gather_probe_ms = 0.0;
                double row_fullres_curve_gather_probe_ms = 0.0;
                double row_ev2raw_store_probe_ms = 0.0;
                double row_arithmetic_probe_ms = 0.0;
                double row_overexposed_density = 0.0;
                double row_cap_clamp_pct = 0.0;
                double row_f_near_0_pct = 0.0;
                double row_f_near_1_pct = 0.0;

                final_blend_row_avx2(raw_row,
                                     image_row,
                                     fullres_row,
                                     fullres_smooth_row,
                                     halfres_smooth_row,
                                     dark_row,
                                     bright_row,
                                     overexposed_row,
                                     NULL,
                                     raw2ev_float, raw2ev, ev2raw, fullres_curve, fullres_curve_f,
                                     black, dark_noise, w,
                                     randn_cache,
                                     &row_k,
                                     fuse_to_16bit,
                                     final_blend_probe_mode,
                                     &row_raw2ev_gather_probe_ms,
                                     &row_fullres_curve_gather_probe_ms,
                                     &row_arithmetic_probe_ms,
                                     &row_ev2raw_store_probe_ms,
                                     &row_overexposed_density,
                                     &row_cap_clamp_pct,
                                     &row_f_near_0_pct,
                                     &row_f_near_1_pct);
                /* tail: pixels not covered by SIMD bulk */
                for (int x = x_start; x < w; x ++) {
                    int b = bright_row[x];
                    int hr = halfres_smooth_row[x];
                    int fr = fullres_row[x];
                    int frs = fullres_smooth_row[x];
                    int hrev = raw2ev[hr];
                    int frev = raw2ev[fr];
                    int frsev = raw2ev[frs];
                    double f = use_float_fullres_curve
                        ? (double)fullres_curve_f[b & 0xFFFFF]
                        : fullres_curve[b & 0xFFFFF];
                    double ovf = COERCE(overexposed_row[x] / 200.0, 0, 1);
                    double c = ovf;
                    double noisy_or_overexposed = MAX(ovf, 1-f);
                    f = MAX(f, c);
                    int sig = (dark_row[x] + bright_row[x]) / 2;
                    double cap_limit = (double)(sig - black) / (4*dark_noise);
                    if (final_blend_probe_detail)
                    {
                        if (ovf > 0.0) row_overexposed_density += 1.0;
                        if (f > cap_limit) row_cap_clamp_pct += 1.0;
                        if (f <= 0.01) row_f_near_0_pct += 1.0;
                        if (f >= 0.99) row_f_near_1_pct += 1.0;
                    }
                    double fev = noisy_or_overexposed * frsev + (1-noisy_or_overexposed) * frev;
                    f = MAX(0, MIN(f, cap_limit));
                    int output = hrev * (1-f) + fev * f;
                    output = COERCE(output, -10*EV_RESOLUTION, 14*EV_RESOLUTION-1);
                    if (fuse_to_16bit)
                    {
                        float dither = randn_cache[row_k & 1023];
                        row_k = (row_k + 1) & 1023;
                        int v = (int)((ev2raw[output] / 16.0f) + dither + 0.5f);
                        image_row[x] = (uint16_t)COERCE(v, 0, 0xFFFF);
                    }
                    else
                    {
                        raw_set_pixel32(x, y, ev2raw[output]);
                    }
                }
                final_blend_raw2ev_gather_probe_ms += row_raw2ev_gather_probe_ms;
                final_blend_fullres_curve_gather_probe_ms += row_fullres_curve_gather_probe_ms;
                final_blend_ev2raw_store_probe_ms += row_ev2raw_store_probe_ms;
                final_blend_arithmetic_probe_ms += row_arithmetic_probe_ms;
                final_blend_overexposed_density += row_overexposed_density;
                final_blend_cap_clamp_pct += row_cap_clamp_pct;
                final_blend_f_near_0_pct += row_f_near_0_pct;
                final_blend_f_near_1_pct += row_f_near_1_pct;
            }
        }

        if (final_blend_probe_detail)
        {
            const double total_pixels = (double)w * (double)h;
            g_dualiso_full20bit_timing.final_blend_overexposed_density = final_blend_overexposed_density / total_pixels;
            g_dualiso_full20bit_timing.final_blend_cap_clamp_pct = final_blend_cap_clamp_pct / total_pixels;
            g_dualiso_full20bit_timing.final_blend_f_near_0_pct = final_blend_f_near_0_pct / total_pixels;
            g_dualiso_full20bit_timing.final_blend_f_near_1_pct = final_blend_f_near_1_pct / total_pixels;
        }
        g_dualiso_full20bit_timing.final_blend_raw2ev_gather_probe_ms += final_blend_raw2ev_gather_probe_ms;
        g_dualiso_full20bit_timing.final_blend_fullres_curve_gather_probe_ms += final_blend_fullres_curve_gather_probe_ms;
        g_dualiso_full20bit_timing.final_blend_ev2raw_store_probe_ms += final_blend_ev2raw_store_probe_ms;
        g_dualiso_full20bit_timing.final_blend_arithmetic_probe_ms += final_blend_arithmetic_probe_ms;
        final_blend_fused_to_16bit = 1;
    }
    else
#endif
    {
        #pragma omp parallel for collapse(2)
        for (int y = 0; y < h; y ++)
        {
            for (int x = 0; x < w; x ++)
            {
                /* high-iso image (for measuring signal level) */
                int b = bright[x + y*w];

                /* half-res image (interpolated and chroma filtered, best for low-contrast shadows) */
                int hr = halfres_smooth[x + y*w];

                /* full-res image (non-interpolated, except where one ISO is blown out) */
                int fr = fullres[x + y*w];

                /* full res with some smoothing applied to hide aliasing artifacts */
                int frs = fullres_smooth[x + y*w];

                /* go from linear to EV space */
                int hrev = raw2ev[hr];
                int frev = raw2ev[fr];
                int frsev = raw2ev[frs];
                int output = 0;

                /* blending factor */
                double f = use_float_fullres_curve
                    ? (double)fullres_curve_f[b & 0xFFFFF]
                    : fullres_curve[b & 0xFFFFF];

                double c = 0;

                if (alias_map)
                {
                    int co = alias_map[x + y*w];
                    c = COERCE(co / (double) ALIAS_MAP_MAX, 0, 1);
                }

                double ovf = COERCE(overexposed[x + y*w] / 200.0, 0, 1);
                c = MAX(c, ovf);

                double noisy_or_overexposed = 0.0;
                if (fullres_mesh_guard)
                {
                    f = MAX(f, c);
                    if (x > 1 && x + 2 < w && y > 1 && y + 2 < h)
                    {
                        const int full_left_ev = raw2ev[fullres[x - 2 + y*w]];
                        const int full_right_ev = raw2ev[fullres[x + 2 + y*w]];
                        const int full_up_ev = raw2ev[fullres[x + (y - 2)*w]];
                        const int full_down_ev = raw2ev[fullres[x + (y + 2)*w]];
                        const int half_left2_ev = raw2ev[halfres_smooth[x - 2 + y*w]];
                        const int half_right2_ev = raw2ev[halfres_smooth[x + 2 + y*w]];
                        const int half_up2_ev = raw2ev[halfres_smooth[x + (y - 2)*w]];
                        const int half_down2_ev = raw2ev[halfres_smooth[x + (y + 2)*w]];
                        const int full_axis_ev = MAX(ABS(full_left_ev - full_right_ev),
                                                     ABS(full_up_ev - full_down_ev));
                        const int half_axis2_ev = MAX(ABS(half_left2_ev - half_right2_ev),
                                                      ABS(half_up2_ev - half_down2_ev));
                        const int full_outlier_ev = MAX(ABS(frev - ((full_left_ev + full_right_ev + 1) / 2)),
                                                        ABS(frev - ((full_up_ev + full_down_ev + 1) / 2)));
                        const int half_outlier_ev = MAX(ABS(hrev - ((half_left2_ev + half_right2_ev + 1) / 2)),
                                                        ABS(hrev - ((half_up2_ev + half_down2_ev + 1) / 2)));
                        if (full_outlier_ev > half_outlier_ev + EV_RESOLUTION / 2 &&
                            full_axis_ev > half_axis2_ev + EV_RESOLUTION / 2)
                        {
                            f = MIN(f, MAX(ovf, 0.005));
                        }
                        else if (full_outlier_ev > half_outlier_ev + EV_RESOLUTION / 4 &&
                                 full_axis_ev > half_axis2_ev + EV_RESOLUTION / 4)
                        {
                            f = MIN(f, MAX(ovf, 0.015));
                        }
                    }
                    if (x > 0 && x + 1 < w && y > 0 && y + 1 < h)
                    {
                        const int half_left_ev = raw2ev[halfres_smooth[x - 1 + y*w]];
                        const int half_right_ev = raw2ev[halfres_smooth[x + 1 + y*w]];
                        const int half_up_ev = raw2ev[halfres_smooth[x + (y - 1)*w]];
                        const int half_down_ev = raw2ev[halfres_smooth[x + (y + 1)*w]];
                        const int half_grad_ev = MAX(ABS(half_left_ev - half_right_ev),
                                                     ABS(half_up_ev - half_down_ev));
                        double local_cap = 1.0;
                        if (half_grad_ev < EV_RESOLUTION / 4)
                        {
                            local_cap = 0.005;
                        }
                        else if (half_grad_ev < EV_RESOLUTION)
                        {
                            local_cap = 0.015;
                        }
                        else if (half_grad_ev < EV_RESOLUTION * 2)
                        {
                            local_cap = 0.04;
                        }
                        else if (half_grad_ev < EV_RESOLUTION * 4)
                        {
                            local_cap = 0.10;
                        }
                        f = MIN(f, MAX(ovf, local_cap));
                    }
                    noisy_or_overexposed = MAX(ovf, 1-f);
                }
                else
                {
                    noisy_or_overexposed = MAX(ovf, 1-f);

                    /* use data from both ISOs in high-detail areas, even if it's noisier (less aliasing) */
                    f = MAX(f, c);
                }

                const int anchor_ev = hrev;

                /* use smoothing in noisy near-overexposed areas to hide color artifacts */
                double fev = noisy_or_overexposed * frsev + (1-noisy_or_overexposed) * frev;
                if (fullres_mesh_guard && x > 1 && x + 2 < w && y > 1 && y + 2 < h)
                {
                    const int full_left_ev = raw2ev[fullres[x - 2 + y*w]];
                    const int full_right_ev = raw2ev[fullres[x + 2 + y*w]];
                    const int full_up_ev = raw2ev[fullres[x + (y - 2)*w]];
                    const int full_down_ev = raw2ev[fullres[x + (y + 2)*w]];
                    const int full_base_ev = (full_left_ev + full_right_ev + full_up_ev + full_down_ev + 2) / 4;
                    const int half_left_ev = raw2ev[halfres_smooth[x - 2 + y*w]];
                    const int half_right_ev = raw2ev[halfres_smooth[x + 2 + y*w]];
                    const int half_up_ev = raw2ev[halfres_smooth[x + (y - 2)*w]];
                    const int half_down_ev = raw2ev[halfres_smooth[x + (y + 2)*w]];
                    const int half_base_ev = (half_left_ev + half_right_ev + half_up_ev + half_down_ev + 2) / 4;
                    const int half_detail_ev = MAX(MAX(ABS(hrev - half_base_ev), ABS(half_left_ev - half_right_ev)),
                                                   ABS(half_up_ev - half_down_ev));
                    const int green_site = ((x ^ y) & 1);
                    const int detail_limit_ev = COERCE(half_detail_ev / 8 + EV_RESOLUTION / 32,
                                                       EV_RESOLUTION / 64,
                                                       EV_RESOLUTION / 4);
                    const int full_detail_ev = green_site
                        ? COERCE(frev - full_base_ev, -detail_limit_ev, detail_limit_ev)
                        : 0;
                    const int full_smooth_detail_ev = green_site
                        ? COERCE(frsev - full_base_ev, -detail_limit_ev, detail_limit_ev)
                        : 0;
                    const int anchored_full_ev = anchor_ev + full_detail_ev;
                    const int anchored_smooth_ev = anchor_ev + full_smooth_detail_ev;
                    fev = noisy_or_overexposed * anchored_smooth_ev + (1-noisy_or_overexposed) * anchored_full_ev;
                }

                /* limit the use of fullres in dark areas (fixes some black spots, but may increase aliasing) */
                int sig = (dark[x + y*w] + bright[x + y*w]) / 2;
                f = MAX(0, MIN(f, (double)(sig - black) / (4*dark_noise)));

                /* blend "half-res" and "full-res" images smoothly to avoid banding*/
                output = anchor_ev * (1-f) + fev * f;

                /* show full-res map (for debugging) */
                //~ output = f * 14*EV_RESOLUTION;

                /* show alias map (for debugging) */
                //~ output = c * 14*EV_RESOLUTION;

                //~ output = hotpixel[x+y*w] ? 14*EV_RESOLUTION : 0;
                //~ output = raw2ev[dark[x+y*w]];
                /* safeguard */
                output = COERCE(output, -10*EV_RESOLUTION, 14*EV_RESOLUTION-1);


                /* back to linear space and commit */
                raw_set_pixel32(x, y, ev2raw[output]);
            }
        }
    }
    g_dualiso_full20bit_timing.final_blend_row_kernel_ms += dualiso_debug_elapsed_ms(final_blend_row_kernel_start);
    return final_blend_fused_to_16bit;
}

static inline void convert_20_to_16bit(struct raw_info raw_info, uint16_t * image_data, uint32_t * raw_buffer_32)
{
    int w = raw_info.width;
    int h = raw_info.height;
    /* go back from 20-bit to 16-bit output */
    //raw_info.buffer = raw_buffer_16;
    raw_info.black_level /= 16;
    raw_info.white_level /= 16;

#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    if (g_dualiso_hq_use_avx2)
    {
        /* Linear-pixel cursor: identical to the scalar coordinate mapping,
         * independent of OpenMP scheduling and row dispatch order. */
        #pragma omp parallel for
        for (int y = 0; y < h; y++) {
            int k = (y * w) & 1023;
            convert_20_to_16bit_row_avx2(&image_data[(size_t)y*w],
                                          &raw_buffer_32[(size_t)y*w],
                                          randn05_cache, &k, w);
        }
        return;
    }
#endif

    #pragma omp parallel for collapse(2)
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            raw_set_pixel_20to16_rand(x, y, raw_buffer_32[x + y*w]);
}

/* P3 diagnostic (gated): FNV-1a-style checksum over a curve, so the recon's
 * actual-used curve at point-of-use can be byte-compared against the published
 * (re-derived) curve without dumping the full 8 MiB array. Sampled stride keeps
 * it cheap on the hot path while still being sensitive to any divergence. */
static uint64_t dualiso_p3_curve_checksum_f64(const double * curve, size_t count)
{
    uint64_t hash = 1469598103934665603ULL; /* FNV offset basis */
    if(!curve) return 0;
    for(size_t i = 0; i < count; i += 257)
    {
        uint64_t bits;
        memcpy(&bits, &curve[i], sizeof(bits));
        hash ^= bits;
        hash *= 1099511628211ULL; /* FNV prime */
    }
    return hash;
}

static uint64_t dualiso_p3_curve_checksum_i32(const int * curve, size_t count)
{
    uint64_t hash = 1469598103934665603ULL;
    if(!curve) return 0;
    for(size_t i = 0; i < count; i += 257)
    {
        hash ^= (uint64_t)(uint32_t)curve[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}

static const double * dualiso_select_gpu_mix_curve(const dualiso_full20bit_scratch_t * scratch,
                                                   uint32_t black,
                                                   uint32_t white,
                                                   double corr_ev,
                                                   double overlap)
{
    if(!scratch) return NULL;

    for(int slot = 0; slot < DUALISO_MIX_CURVE_CACHE_SLOTS; ++slot)
    {
        if(scratch->mix_curve_valid[slot]
        && scratch->mix_curve[slot]
        && scratch->mix_curve_last_black[slot] == black
        && scratch->mix_curve_last_white[slot] == white
        && scratch->mix_curve_last_corr_ev[slot] == corr_ev
        && scratch->mix_curve_last_overlap[slot] == overlap)
        {
            return scratch->mix_curve[slot];
        }
    }

    return NULL;
}

static void dualiso_debug_publish_gpu_recon_state(int ret,
                                                  int rggb,
                                                  struct raw_info raw_info,
                                                  uint32_t black,
                                                  uint32_t white,
                                                  uint32_t white_darkened,
                                                  const int is_bright[4],
                                                  double corr_ev,
                                                  int black_delta_14,
                                                  double dark_noise,
                                                  int interp_method,
                                                  int use_alias_map,
                                                  int use_fullres,
                                                  int chroma_smooth_method,
                                                  int playback_preview_scale_factor,
                                                  int final_blend_fused_to_16bit,
                                                  dualiso_full20bit_scratch_t * scratch)
{
    if(!g_dualiso_gpu_recon_state_capture_enabled)
    {
        return;
    }

    dualiso_gpu_recon_state_t state;
    memset(&state, 0, sizeof(state));

    if(!ret || !rggb || !scratch || !is_bright)
    {
        g_dualiso_last_gpu_recon_state = state;
        return;
    }

    if(interp_method != 1 || !use_fullres || chroma_smooth_method > 1)
    {
        g_dualiso_last_gpu_recon_state = state;
        return;
    }

    const double overlap = g_dualiso_full20bit_timing.mix_curve_overlap;
    const double * mix_curve = dualiso_select_gpu_mix_curve(scratch, black, white, corr_ev, overlap);
    const double * fullres_curve = build_fullres_curve_float_as_double(scratch, (int)black);

    if(!scratch->ev_raw2ev || !scratch->ev2raw_0 || !mix_curve || !fullres_curve)
    {
        g_dualiso_last_gpu_recon_state = state;
        return;
    }

    state.valid = 1;
    state.width = raw_info.width;
    state.height = raw_info.height;
    state.black_level = (int)black;
    state.white_level = (int)white;
    state.white_darkened = (int)white_darkened;
    state.black_delta = black_delta_14 * 64;
    state.ev_correction = corr_ev;
    state.dark_noise = dark_noise;
    state.interp_method = interp_method;
    state.use_alias_map = use_alias_map != 0;
    state.use_fullres = use_fullres != 0;
    state.chroma_smooth_method = chroma_smooth_method;
    state.playback_preview_scale_factor = playback_preview_scale_factor;
    memcpy(state.is_bright, is_bright, sizeof(state.is_bright));
    state.raw2ev = scratch->ev_raw2ev;
    state.ev2raw = scratch->ev2raw_0;
    state.mix_curve = mix_curve;
    state.fullres_curve = fullres_curve;
    state.randn05 = final_blend_fused_to_16bit ? randn05_cache : NULL;
    state.apply_dither = final_blend_fused_to_16bit != 0;

    g_dualiso_last_gpu_recon_state = state;
}

static int dualiso_final_blend_will_fuse_to_16bit(int fullres_mesh_guard)
{
#ifdef DUALISO_AVX2_AVAILABLE
    pthread_once(&g_dualiso_hq_dispatch_once, dualiso_hq_dispatch_init);
    return g_dualiso_hq_use_avx2 != 0 && !fullres_mesh_guard;
#else
    (void)fullres_mesh_guard;
    return 0;
#endif
}

/* ---- BUG-A fix: per-frame dual-ISO phase verification (work order
 * .claude-state/coordination/bug-a-recon-work-order.md). The bright/dark row
 * pattern of some real dual-ISO clips ROTATES per camera frame; the cached
 * diso_pattern is only a prior and must be verified against each frame. ---- */

static int g_dualiso_phase_verify_enabled_cache = -1;
static int g_dualiso_phase_log_enabled_cache = -1;

void dualiso_debug_reset_phase_verify_env_cache(void)
{
    g_dualiso_phase_verify_enabled_cache = -1;
    g_dualiso_phase_log_enabled_cache = -1;
}

static int dualiso_phase_verify_enabled(void)
{
    if (g_dualiso_phase_verify_enabled_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISO_PER_FRAME_PHASE");
        g_dualiso_phase_verify_enabled_cache = (v && v[0] == '0') ? 0 : 1;   /* default ON */
    }
    return g_dualiso_phase_verify_enabled_cache;
}

static int dualiso_phase_log_enabled(void)
{
    if (g_dualiso_phase_log_enabled_cache < 0)
    {
        const char * v = getenv("MLVAPP_LOG_DISO_PHASE");
        g_dualiso_phase_log_enabled_cache = (v && v[0] && v[0] != '0') ? 1 : 0;
    }
    return g_dualiso_phase_log_enabled_cache;
}

/* Cheap read-only phase probe: subsampled mod-4 green-pixel row-group means.
 * Returns 1 with *implied_pattern set to the iso_patterns index (0..3) when
 * THIS frame shows a DECISIVE 2-line dual-ISO layout; returns 0 otherwise
 * (low signal / non-cyclic top pair / tiny frame -> caller keeps the cache).
 * Cost ~w*h/64 reads (~48K at 1920x804, well under 0.3ms). Domain note: runs
 * BEFORE the 14->20 bit black/white scaling in both callers, so
 * raw_info.black_level / white_level are the raw 14-bit-domain values. */
static int dualiso_phase_probe(struct raw_info raw_info,
                               uint16_t * image_data,
                               int * implied_pattern)
{
    const int w = raw_info.width;
    const int h = raw_info.height;
    const int y0 = (raw_info.active_area.y1 + 3) & ~3;   /* same convention as identify */
    const int y_end = h / 4 * 4;
    if (y_end - y0 < 16 || w < 16) return 0;

    double sum[4] = {0.0, 0.0, 0.0, 0.0};
    long   cnt[4] = {0, 0, 0, 0};

    for (int y = y0; y < y_end; y++)
    {
        if (((y - y0) >> 2) & 3) continue;               /* every 4th row-quad */
        const uint16_t * row = image_data + (size_t)y * (size_t)w;
        for (int x = ((y % 2) ? 0 : 1); x < w; x += 8)   /* green sites, every 4th green */
        {
            sum[y % 4] += (double)row[x];
            cnt[y % 4]++;
        }
    }

    double g[4];
    for (int i = 0; i < 4; i++)
    {
        if (cnt[i] < 64) return 0;
        g[i] = sum[i] / (double)cnt[i] - (double)raw_info.black_level;
    }

    int order[4] = {0, 1, 2, 3};
    for (int i = 0; i < 4; i++)
        for (int j = i + 1; j < 4; j++)
            if (g[order[j]] > g[order[i]]) { int t = order[i]; order[i] = order[j]; order[j] = t; }

    const int b0 = order[0];
    const int b1 = order[1];
    /* bright pair must be cyclically adjacent: {0,1},{1,2},{2,3},{3,0} */
    if (!((((b0 + 1) & 3) == b1) || (((b1 + 1) & 3) == b0))) return 0;

    const double bright_min = MIN(g[b0], g[b1]);
    const double dark_max   = MAX(g[order[2]], g[order[3]]);
    const double margin_floor =
        MAX(16.0, (double)(raw_info.white_level - raw_info.black_level) / 64.0);
    if (bright_min - dark_max < margin_floor) return 0;

    int probe_bright[4] = {0, 0, 0, 0};
    probe_bright[b0] = 1;
    probe_bright[b1] = 1;
    static const int probe_patterns[4][4] = {{1,1,0,0},{1,0,0,1},{0,0,1,1},{0,1,1,0}};
    for (int i = 0; i < 4; i++)
    {
        if (probe_bright[0] == probe_patterns[i][0] &&
            probe_bright[1] == probe_patterns[i][1] &&
            probe_bright[2] == probe_patterns[i][2] &&
            probe_bright[3] == probe_patterns[i][3])
        {
            *implied_pattern = i;
            return 1;
        }
    }
    return 0;
}

/* Factored pattern resolution shared by diso_get_full20bit and
 * diso_prepare_gpu_recon_state. Fills is_bright[4]; returns 0 = hard failure
 * (caller returns 0 exactly as before). Behavior is IDENTICAL to the pre-fix
 * duplicated blocks EXCEPT inside the cached -1..-4 and explicit +1..+4
 * branches, where the selected pattern is verified against THIS frame and
 * re-detected on decisive mismatch (fail-safe: any probe/identify failure
 * keeps the selected pattern). */
static int dualiso_resolve_iso_pattern(struct raw_info raw_info,
                                       uint16_t * image_data,
                                       int rggb,
                                       int * iso_pattern,
                                       int * is_bright,
                                       dualiso_full20bit_scratch_t * scratch)
{
    const int iso_patterns[4][4] = {{1, 1, 0, 0}, {1, 0, 0, 1}, {0, 0, 1, 1}, {0, 1, 1, 0}};
    const int initial_iso_pattern = iso_pattern ? *iso_pattern : 0;
    g_dualiso_full20bit_timing.pattern_initial = initial_iso_pattern;
    g_dualiso_full20bit_timing.pattern_resolved = initial_iso_pattern;
    g_dualiso_full20bit_timing.pattern_source =
        DUALISO_FULL20_PATTERN_SOURCE_NONE;
    g_dualiso_full20bit_timing.pattern_result = 0;
    g_dualiso_full20bit_timing.phase_verify_enabled =
        dualiso_phase_verify_enabled() ? 1 : 0;
    g_dualiso_full20bit_timing.phase_probe_attempted = 0;
    g_dualiso_full20bit_timing.phase_probe_succeeded = 0;
    g_dualiso_full20bit_timing.phase_probe_decisive = 0;
    g_dualiso_full20bit_timing.phase_probe_redetected = 0;
    g_dualiso_full20bit_timing.phase_cached_pattern = 0;
    g_dualiso_full20bit_timing.phase_implied_pattern = 0;

    if (!*iso_pattern)
    {
        if (!identify_bright_and_dark_fields(raw_info, image_data, rggb, is_bright, scratch))
        {
            dualiso_debug_note_pattern_resolution(
                DUALISO_FULL20_PATTERN_SOURCE_FRESH_AUTO,
                iso_pattern,
                NULL);
            return 0;
        }
        for (int i = 0; i < 4; i++)
        {
            if (memcmp(is_bright, iso_patterns[i], 4 * sizeof(int)) == 0)
            {
                *iso_pattern = -(i + 1);
                break;
            }
        }
        dualiso_debug_note_pattern_resolution(
            DUALISO_FULL20_PATTERN_SOURCE_FRESH_AUTO,
            iso_pattern,
            is_bright);
        return (*iso_pattern != 0);
    }
    else if (*iso_pattern > 0 && *iso_pattern <= 4)
    {
        const int explicit_idx = *iso_pattern - 1;
        g_dualiso_full20bit_timing.phase_cached_pattern = explicit_idx + 1;
        if (g_dualiso_full20bit_timing.phase_verify_enabled)
        {
            int implied = -1;
            g_dualiso_full20bit_timing.phase_probe_attempted = 1;
            if (dualiso_phase_probe(raw_info, image_data, &implied))
            {
                g_dualiso_full20bit_timing.phase_probe_succeeded = 1;
                if (implied >= 0)
                    g_dualiso_full20bit_timing.phase_implied_pattern = implied + 1;
                if (implied != explicit_idx)
                {
                    g_dualiso_full20bit_timing.phase_probe_decisive = 1;
                    if (dualiso_phase_log_enabled())
                    {
                        fprintf(stderr,
                                "[DISO_PHASE] explicit mismatch: explicit=%d implied=%d\n",
                                explicit_idx, implied);
                    }
                }
            }
        }
        memcpy(is_bright, iso_patterns[explicit_idx], 4 * sizeof(int));
        dualiso_debug_note_pattern_resolution(
            DUALISO_FULL20_PATTERN_SOURCE_EXPLICIT_POSITIVE,
            iso_pattern,
            is_bright);
        return 1;
    }
    else if (*iso_pattern >= -4 && *iso_pattern <= -1)
    {
        const int cached_idx = (-*iso_pattern) - 1;
        g_dualiso_full20bit_timing.phase_cached_pattern = cached_idx + 1;
        if (g_dualiso_full20bit_timing.phase_verify_enabled)
        {
            int implied = -1;
            g_dualiso_full20bit_timing.phase_probe_attempted = 1;
            if (dualiso_phase_probe(raw_info, image_data, &implied))
            {
                g_dualiso_full20bit_timing.phase_probe_succeeded = 1;
                if (implied >= 0)
                    g_dualiso_full20bit_timing.phase_implied_pattern = implied + 1;
                if (implied != cached_idx)
                {
                    int fresh[4];
                    g_dualiso_full20bit_timing.phase_probe_decisive = 1;
                    if (identify_bright_and_dark_fields(raw_info, image_data, rggb, fresh, scratch))
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            if (memcmp(fresh, iso_patterns[i], 4 * sizeof(int)) == 0)
                            {
                                if (dualiso_phase_log_enabled())
                                {
                                    fprintf(stderr,
                                            "[DISO_PHASE] per-frame re-detect: cached=%d implied=%d fresh=%d\n",
                                            cached_idx, implied, i);
                                }
                                memcpy(is_bright, fresh, 4 * sizeof(int));
                                *iso_pattern = -(i + 1);
                                g_dualiso_full20bit_timing.phase_probe_redetected = 1;
                                dualiso_debug_note_pattern_resolution(
                                    DUALISO_FULL20_PATTERN_SOURCE_CACHED_NEGATIVE_REDETECTED,
                                    iso_pattern,
                                    is_bright);
                                return 1;
                            }
                        }
                    }
                    /* identify failed or non-tabular -> fall through to cache */
                }
            }
        }
        memcpy(is_bright, iso_patterns[cached_idx], 4 * sizeof(int));
        dualiso_debug_note_pattern_resolution(
            DUALISO_FULL20_PATTERN_SOURCE_CACHED_NEGATIVE,
            iso_pattern,
            is_bright);
        return 1;
    }
    else if (*iso_pattern == 5)
    {
        int source = DUALISO_FULL20_PATTERN_SOURCE_SPECIAL_AUTO_DETECT;
        if (!identify_bright_and_dark_fields(raw_info, image_data, rggb, is_bright, scratch))
        {
            memcpy(is_bright, iso_patterns[0], 4 * sizeof(int));
            source = DUALISO_FULL20_PATTERN_SOURCE_SPECIAL_AUTO_DEFAULT;
        }
        dualiso_debug_note_pattern_resolution(source, iso_pattern, is_bright);
        return 1;
    }
    dualiso_debug_note_pattern_resolution(
        DUALISO_FULL20_PATTERN_SOURCE_INVALID,
        iso_pattern,
        NULL);
    return 0;
}

int diso_prepare_gpu_recon_state(struct raw_info raw_info,
                                 uint16_t * image_data,
                                 int dark_frame,
                                 int iso1,
                                 int iso2,
                                 int * iso_pattern,
                                 int * auto_correction,
                                 double * ev_correction,
                                 int * black_delta,
                                 int interp_method,
                                 int use_alias_map,
                                 int use_fullres,
                                 int chroma_smooth_method,
                                 int playback_preview_scale_factor,
                                 int threads,
                                 dualiso_full20bit_scratch_t * scratch,
                                 dualiso_gpu_recon_state_t * state)
{
    const double full20_start = mlv_stage_timing_now();
    int restore_iso_pattern_on_failure = 0;
    int initial_iso_pattern = 0;
    dualiso_debug_reset_full20bit_timing();
    dualiso_debug_set_full20bit_path(DUALISO_FULL20_PATH_GPU_PREPARE,
                                     raw_info,
                                     interp_method,
                                     use_alias_map,
                                     use_fullres,
                                     playback_preview_scale_factor,
                                     threads);
#define DUALISO_GPU_PREP_RETURN(value) do { \
    const int dualiso_return_value = (value); \
    if (!dualiso_return_value && restore_iso_pattern_on_failure && iso_pattern) \
        *iso_pattern = initial_iso_pattern; \
    dualiso_debug_finish_full20bit_timing(full20_start); \
    return dualiso_return_value; \
} while (0)

    if (state)
    {
        memset(state, 0, sizeof(*state));
    }
    if (!state || !image_data || !iso_pattern || !auto_correction || !ev_correction
     || !black_delta || !scratch)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    initial_iso_pattern = *iso_pattern;
    restore_iso_pattern_on_failure = 1;
    int staged_auto_correction = *auto_correction;
    double staged_ev_correction = *ev_correction;
    int staged_black_delta = *black_delta;

    int w = raw_info.width;
    int h = raw_info.height;
    const int effective_chroma_smooth_method =
        dualiso_supported_chroma_smooth_method(chroma_smooth_method);

    if (interp_method != 1 || !use_fullres || effective_chroma_smooth_method > 1)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }

    const int rggb = ((raw_info.cfa_pattern == 0) || (raw_info.cfa_pattern == 0x02010100)) ? 1 : 0;
    if (!rggb)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }

    const int requires_full20_match = (staged_auto_correction == -2);
    size_t checked_full20_pixels = 0;
    if (!dualiso_checked_full20_frame_geometry(raw_info, &checked_full20_pixels))
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    if (requires_full20_match)
    {
        size_t checked_sample_capacity = 0;
        size_t checked_highlight_capacity = 0;
        int checked_first_sample_y = 0;
        if (!dualiso_checked_histogram_geometry(w,
                                                h,
                                                raw_info.active_area.y1,
                                                &checked_full20_pixels,
                                                &checked_sample_capacity,
                                                &checked_highlight_capacity,
                                                &checked_first_sample_y))
        {
            DUALISO_GPU_PREP_RETURN(0);
        }
    }

    int is_bright[4];
    double stage_start = mlv_stage_timing_now();

    if (!dualiso_resolve_iso_pattern(raw_info, image_data, rggb, iso_pattern, is_bright, scratch))
    {
        g_dualiso_full20bit_timing.pattern_ms += dualiso_debug_elapsed_ms(stage_start);
        DUALISO_GPU_PREP_RETURN(0);
    }
    g_dualiso_full20bit_timing.pattern_ms += dualiso_debug_elapsed_ms(stage_start);

    raw_info.black_level *= 64;
    raw_info.white_level *= 64;

    const int black = raw_info.black_level;
    int white = raw_info.white_level / 64;
    int white_bright = white / 2;
    white *= 64;
    white_bright *= 64;
    raw_info.white_level = white;

    double noise_std[4];
    double dark_noise = 0.0;
    double bright_noise = 0.0;
    double dark_noise_ev = 0.0;
    double bright_noise_ev = 0.0;
    stage_start = mlv_stage_timing_now();
    (void)compute_noise(raw_info,
                        image_data,
                        noise_std,
                        &dark_noise,
                        &bright_noise,
                        &dark_noise_ev,
                        &bright_noise_ev);
    g_dualiso_full20bit_timing.noise_ms += dualiso_debug_elapsed_ms(stage_start);

    if (!isfinite(dark_noise)
        || !isfinite(bright_noise)
        || !isfinite(dark_noise_ev)
        || !isfinite(bright_noise_ev)
        || dark_noise <= 0.0
        || bright_noise <= 0.0)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }

    dark_noise *= 64;
    bright_noise *= 64;
    dark_noise_ev += 6;
    bright_noise_ev += 6;

    int white_darkened = white_bright;
    uint32_t * raw_buffer_32 = NULL;
    if (requires_full20_match)
    {
        stage_start = mlv_stage_timing_now();
        if (!ensure_full20bit_pixel_capacity(scratch, checked_full20_pixels))
        {
            g_dualiso_full20bit_timing.scratch_ms += dualiso_debug_elapsed_ms(stage_start);
            DUALISO_GPU_PREP_RETURN(0);
        }
        g_dualiso_full20bit_timing.scratch_ms += dualiso_debug_elapsed_ms(stage_start);

        stage_start = mlv_stage_timing_now();
        raw_buffer_32 = convert_to_20bit(raw_info, image_data, scratch->raw_buffer_32);
        g_dualiso_full20bit_timing.convert20_ms += dualiso_debug_elapsed_ms(stage_start);
        if (!raw_buffer_32)
        {
            DUALISO_GPU_PREP_RETURN(0);
        }
    }

    stage_start = mlv_stage_timing_now();
    const int expo_matched = requires_full20_match
        ? match_exposures(raw_info,
                          raw_buffer_32,
                          dark_frame,
                          iso1,
                          iso2,
                          &staged_auto_correction,
                          &staged_ev_correction,
                          &staged_black_delta,
                          &white_darkened,
                          is_bright,
                          scratch)
        : compute_match_exposure_scalars(raw_info,
                                         NULL,
                                         dark_frame,
                                         iso1,
                                         iso2,
                                         &staged_auto_correction,
                                         &staged_ev_correction,
                                         &staged_black_delta,
                                         &white_darkened,
                                         is_bright,
                                         scratch,
                                         0,
                                         NULL,
                                         NULL);
    if (expo_matched <= 0)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    const double corr_ev = ABS(staged_ev_correction);
    g_dualiso_full20bit_timing.match_ms += dualiso_debug_elapsed_ms(stage_start);

    const double lowiso_dr = log2(white - black) - dark_noise_ev;
    if (!isfinite(lowiso_dr))
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    double * mix_curve = NULL;
    int mix_curve_slot = 0;
    int * raw2ev = NULL;
    float * raw2ev_float = NULL;
    int * ev2raw = NULL;
    if (!prepare_mix_curve_and_ev_lut((uint32_t)black,
                                      (uint32_t)white,
                                      corr_ev,
                                      lowiso_dr,
                                      scratch,
                                      0,
                                      &mix_curve,
                                      &mix_curve_slot,
                                      &raw2ev,
                                      &raw2ev_float,
                                      &ev2raw))
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    (void)mix_curve_slot;
    (void)raw2ev;
    (void)raw2ev_float;
    (void)ev2raw;

    const double * fullres_curve = build_fullres_curve_float_as_double(scratch, black);
    if (!scratch->ev_raw2ev || !scratch->ev2raw_0 || !mix_curve || !fullres_curve)
    {
        DUALISO_GPU_PREP_RETURN(0);
    }
    const int fullres_mesh_guard =
        dualiso_x1_playback_fullres_mesh_fix_enabled(playback_preview_scale_factor,
                                                     use_alias_map,
                                                     use_fullres,
                                                     effective_chroma_smooth_method);

    *auto_correction = staged_auto_correction;
    *ev_correction = staged_ev_correction;
    *black_delta = staged_black_delta;
    restore_iso_pattern_on_failure = 0;
    state->valid = 1;
    state->width = raw_info.width;
    state->height = raw_info.height;
    state->black_level = black;
    state->white_level = white;
    state->white_darkened = white_darkened;
    state->black_delta = staged_black_delta * 64;
    state->ev_correction = corr_ev;
    state->dark_noise = dark_noise;
    state->interp_method = interp_method;
    state->use_alias_map = use_alias_map != 0;
    state->use_fullres = use_fullres != 0;
    state->chroma_smooth_method = effective_chroma_smooth_method;
    state->playback_preview_scale_factor = playback_preview_scale_factor;
    memcpy(state->is_bright, is_bright, sizeof(state->is_bright));
    state->raw2ev = scratch->ev_raw2ev;
    state->ev2raw = scratch->ev2raw_0;
    state->mix_curve = mix_curve;
    state->fullres_curve = fullres_curve;
    state->apply_dither = dualiso_final_blend_will_fuse_to_16bit(fullres_mesh_guard);
    state->randn05 = state->apply_dither ? randn05_cache : NULL;

    g_dualiso_full20bit_timing.valid = 1;
    DUALISO_GPU_PREP_RETURN(1);
#undef DUALISO_GPU_PREP_RETURN
}

int diso_get_full20bit(struct raw_info raw_info, uint16_t * image_data, int dark_frame, int iso1, int iso2, int * iso_pattern, int * auto_correction, double * ev_correction, int * black_delta, int interp_method, int use_alias_map, int use_fullres, int chroma_smooth_method, int playback_preview_scale_factor, int threads, dualiso_full20bit_scratch_t * scratch)
{
    const double full20_start = mlv_stage_timing_now();
    int restore_iso_pattern_on_failure = 0;
    int initial_iso_pattern = 0;
    dualiso_debug_reset_full20bit_timing();
    dualiso_debug_set_full20bit_path(DUALISO_FULL20_PATH_CPU_FULL20,
                                     raw_info,
                                     interp_method,
                                     use_alias_map,
                                     use_fullres,
                                     playback_preview_scale_factor,
                                     threads);
#define DUALISO_FULL20_RETURN(value) do { \
    const int dualiso_return_value = (value); \
    if (!dualiso_return_value && restore_iso_pattern_on_failure && iso_pattern) \
        *iso_pattern = initial_iso_pattern; \
    dualiso_debug_finish_full20bit_timing(full20_start); \
    return dualiso_return_value; \
} while (0)

    if (!image_data || !iso_pattern || !auto_correction || !ev_correction
     || !black_delta || !scratch)
    {
        DUALISO_FULL20_RETURN(0);
    }
    initial_iso_pattern = *iso_pattern;
    restore_iso_pattern_on_failure = 1;
    int staged_auto_correction = *auto_correction;
    double staged_ev_correction = *ev_correction;
    int staged_black_delta = *black_delta;

    int w = raw_info.width;
    int h = raw_info.height;
    const int effective_chroma_smooth_method =
        dualiso_supported_chroma_smooth_method(chroma_smooth_method);

    if ((interp_method != 0 && interp_method != 1)
        || (interp_method == 0 && threads <= 0))
    {
        DUALISO_FULL20_RETURN(0);
    }
    
    size_t original_full20_pixels = 0;
    if (!dualiso_checked_full20_frame_geometry(raw_info, &original_full20_pixels))
    {
        DUALISO_FULL20_RETURN(0);
    }

    /* RGGB or GBRG? */
    //int rggb = identify_rggb_or_gbrg(raw_info, image_data, scratch);
    const int rggb = ((raw_info.cfa_pattern == 0)
                   || (raw_info.cfa_pattern == 0x02010100)) ? 1 : 0;
    const int gbrg = (raw_info.cfa_pattern == 0x01000201) ? 1 : 0;

    if (!rggb && !gbrg)
    {
        DUALISO_FULL20_RETURN(0);
    }

    if (!rggb) /* this code assumes RGGB, so we need to skip one line */
    {
        if (raw_info.pitch != raw_info.width
            || (size_t)raw_info.pitch >= original_full20_pixels
            || raw_info.active_area.y1 == INT_MAX
            || raw_info.active_area.y2 == INT_MIN
            || raw_info.height <= 1)
        {
            DUALISO_FULL20_RETURN(0);
        }
        image_data += raw_info.pitch;
        raw_info.active_area.y1++;
        raw_info.active_area.y2--;
        raw_info.height--;
        h--;
    }

    size_t checked_full20_pixels = 0;
    if (!dualiso_checked_full20_frame_geometry(raw_info, &checked_full20_pixels))
    {
        DUALISO_FULL20_RETURN(0);
    }
    /* AMaZE fills its 16-pixel top/left corners from source coordinates
     * 32..17, so both dimensions must contain index 32.  Mean23 does not
     * use that reflection border and retains the smaller shared minimum. */
    if (interp_method == 0 && (raw_info.width < 33 || raw_info.height < 33))
    {
        DUALISO_FULL20_RETURN(0);
    }
    if (staged_auto_correction == -2)
    {
        size_t checked_sample_capacity = 0;
        size_t checked_highlight_capacity = 0;
        int checked_first_sample_y = 0;
        if (!dualiso_checked_histogram_geometry(w,
                                                h,
                                                raw_info.active_area.y1,
                                                &checked_full20_pixels,
                                                &checked_sample_capacity,
                                                &checked_highlight_capacity,
                                                &checked_first_sample_y))
        {
            DUALISO_FULL20_RETURN(0);
        }
    }
    
    int is_bright[4];
    double stage_start = mlv_stage_timing_now();

    if (!dualiso_resolve_iso_pattern(raw_info, image_data, rggb, iso_pattern, is_bright, scratch))
    {
        g_dualiso_full20bit_timing.pattern_ms += dualiso_debug_elapsed_ms(stage_start);
        DUALISO_FULL20_RETURN(0);
    }
    g_dualiso_full20bit_timing.pattern_ms += dualiso_debug_elapsed_ms(stage_start);
    
    int ret = 0;
    
    /* will use 20-bit processing and 16-bit output, instead of 14 */
    raw_info.black_level *= 64;
    raw_info.white_level *= 64;
    
    int black = raw_info.black_level;
    int white = raw_info.white_level / 64;
    
    int white_bright = white / 2;
    //white_detect(raw_info, image_data, &white, &white_bright, is_bright);
    white *= 64;
    white_bright *= 64;
    raw_info.white_level = white;
    
    double noise_std[4];
    double dark_noise, bright_noise, dark_noise_ev, bright_noise_ev;
    stage_start = mlv_stage_timing_now();
    double noise_avg = compute_noise(raw_info, image_data, noise_std, &dark_noise, &bright_noise, &dark_noise_ev, &bright_noise_ev);
    g_dualiso_full20bit_timing.noise_ms += dualiso_debug_elapsed_ms(stage_start);

    if (!isfinite(noise_avg)
        || !isfinite(dark_noise)
        || !isfinite(bright_noise)
        || !isfinite(dark_noise_ev)
        || !isfinite(bright_noise_ev)
        || dark_noise <= 0.0
        || bright_noise <= 0.0)
    {
        DUALISO_FULL20_RETURN(0);
    }

    stage_start = mlv_stage_timing_now();
    if (!ensure_full20bit_pixel_capacity(scratch, checked_full20_pixels))
    {
        g_dualiso_full20bit_timing.scratch_ms += dualiso_debug_elapsed_ms(stage_start);
        DUALISO_FULL20_RETURN(0);
    }
    g_dualiso_full20bit_timing.scratch_ms += dualiso_debug_elapsed_ms(stage_start);
    
    /* promote from 14 to 20 bits (original raw buffer holds 14-bit values stored as uint16_t) */
    stage_start = mlv_stage_timing_now();
    uint32_t * raw_buffer_32 = convert_to_20bit(raw_info, image_data, scratch->raw_buffer_32);
    g_dualiso_full20bit_timing.convert20_ms += dualiso_debug_elapsed_ms(stage_start);
    if (!raw_buffer_32)
    {
        DUALISO_FULL20_RETURN(0);
    }
    
    /* we have now switched to 20-bit, update noise numbers */
    dark_noise *= 64;
    bright_noise *= 64;
    dark_noise_ev += 6;
    bright_noise_ev += 6;
    
    /* dark and bright exposures, interpolated */
    stage_start = mlv_stage_timing_now();
    uint32_t* dark = scratch->dark;
    uint32_t* bright = scratch->bright;
    
    /* fullres image (minimizes aliasing) */
    uint32_t* fullres = scratch->fullres;
    uint32_t* fullres_smooth = fullres;

    /* halfres image (minimizes noise and banding) */
    uint32_t* halfres = scratch->halfres;
    uint32_t* halfres_smooth = halfres;

    uint32_t* dark_for_mix = dark;
    uint32_t* bright_for_mix = bright;
    
    /* overexposure map */
    uint16_t * overexposed = scratch->overexposed;
    
    uint16_t* alias_map = NULL;
    if (use_alias_map)
    {
        alias_map = scratch->alias_map;
        g_dualiso_alias_map_taken_count++;
    }

    const int pixel_count = w * h;
    const int clear_interpolation_buffers = (interp_method == 0);
    const int clear_fullres_buffer = !use_fullres;
    if (clear_interpolation_buffers || clear_fullres_buffer || alias_map)
    {
        #pragma omp parallel for
        for (int i = 0; i < pixel_count; i++)
        {
            if (clear_interpolation_buffers)
            {
                dark[i] = 0;
                bright[i] = 0;
            }
            if (clear_fullres_buffer)
            {
                fullres[i] = 0;
            }
            if (alias_map)
            {
                alias_map[i] = 0;
            }
        }
    }
    g_dualiso_full20bit_timing.scratch_ms += dualiso_debug_elapsed_ms(stage_start);
    
    //~ printf("Exposure matching...\n");
    /* estimate ISO difference between bright and dark exposures */
    int white_darkened = white_bright;
    stage_start = mlv_stage_timing_now();
    int expo_matched = match_exposures(raw_info,
                                       raw_buffer_32,
                                       dark_frame,
                                       iso1,
                                       iso2,
                                       &staged_auto_correction,
                                       &staged_ev_correction,
                                       &staged_black_delta,
                                       &white_darkened,
                                       is_bright,
                                       scratch);
    if (expo_matched <= 0 || g_dualiso_match_probe_stop_after_match)
    {
        DUALISO_FULL20_RETURN(0);
    }
    double corr_ev = ABS(staged_ev_correction);
    g_dualiso_full20bit_timing.match_ms += dualiso_debug_elapsed_ms(stage_start);

#ifndef STDOUT_SILENT
    if (expo_matched)
    {
        printf("Exposures matched");
    }
    else
    {
        printf("Exposures not matched");
    }
#endif
    /* estimate dynamic range */
    double lowiso_dr = log2(white - black) - dark_noise_ev;
    if (!isfinite(lowiso_dr))
    {
        DUALISO_FULL20_RETURN(0);
    }
#ifndef STDOUT_SILENT
    double highiso_dr = log2(white_bright - black) - bright_noise_ev;
    printf("Dynamic range   : %.02f (+) %.02f => %.02f EV (in theory)\n", lowiso_dr, highiso_dr, highiso_dr + corr_ev);
#endif
    /* correction factor for the bright exposure, which was just darkened */
    //double factor = pow(2, corr_ev);

    /* update bright noise measurements, so they can be compared after scaling */
    //bright_noise /= factor;
    //bright_noise_ev -= corr_ev;

    stage_start = mlv_stage_timing_now();
    if (interp_method == 0)
    {
        double interp_stage_start = mlv_stage_timing_now();
        dualiso_debug_note_hq_path(0);
        if (!amaze_interpolate(raw_info,
                               raw_buffer_32,
                               dark,
                               bright,
                               black,
                               white,
                               white_darkened,
                               is_bright,
                               threads,
                               scratch))
        {
            DUALISO_FULL20_RETURN(0);
        }
        g_dualiso_full20bit_timing.interp_amaze_ms +=
            dualiso_debug_elapsed_ms(interp_stage_start);
    }
    else
    {
        double interp_stage_start = mlv_stage_timing_now();
        dualiso_debug_note_hq_path(1);
        mean23_interpolate(raw_info,
                           raw_buffer_32,
                           dark,
                           bright,
                           black,
                           white,
                           white_darkened,
                           is_bright,
                           scratch);
        g_dualiso_full20bit_timing.interp_mean23_ms +=
            dualiso_debug_elapsed_ms(interp_stage_start);
    }

    double interp_border_start = mlv_stage_timing_now();
    border_interpolate(raw_info, raw_buffer_32, dark, bright, is_bright);
    g_dualiso_full20bit_timing.interp_border_ms +=
        dualiso_debug_elapsed_ms(interp_border_start);
    g_dualiso_full20bit_timing.interp_ms += dualiso_debug_elapsed_ms(stage_start);

    const int fullres_mesh_guard =
        dualiso_x1_playback_fullres_mesh_fix_enabled(playback_preview_scale_factor,
                                                     use_alias_map,
                                                     use_fullres,
                                                     effective_chroma_smooth_method);

    if (effective_chroma_smooth_method)
    {
        if (!smooth_matched_source_planes(raw_info,
                                          dark,
                                          bright,
                                          scratch->halfres_smooth,
                                          scratch->fullres_smooth,
                                          effective_chroma_smooth_method,
                                          black,
                                          white,
                                          white_darkened,
                                          scratch))
        {
            DUALISO_FULL20_RETURN(0);
        }
        dark_for_mix = scratch->halfres_smooth;
        bright_for_mix = scratch->fullres_smooth;

    }

    if (use_fullres)
    {
        g_dualiso_fullres_blend_taken_count++;
        stage_start = mlv_stage_timing_now();
        if (effective_chroma_smooth_method)
        {
            int * fullres_raw2ev = NULL;
            float * fullres_raw2ev_float = NULL;
            int * fullres_ev2raw = NULL;
            if (!ensure_scratch_ev_lut(scratch, black, white, &fullres_raw2ev, &fullres_raw2ev_float, &fullres_ev2raw))
            {
                DUALISO_FULL20_RETURN(0);
            }
            (void)fullres_raw2ev_float;
            fullres_reconstruction_phase_balance(raw_info,
                                                 fullres,
                                                 dark,
                                                 bright,
                                                 white_darkened,
                                                 is_bright,
                                                 fullres_raw2ev,
                                                 fullres_ev2raw);
        }
        else
        {
            fullres_reconstruction(raw_info, fullres, dark_for_mix, bright_for_mix, white_darkened, is_bright);
        }
        g_dualiso_full20bit_timing.fullres_ms += dualiso_debug_elapsed_ms(stage_start);
    }

    stage_start = mlv_stage_timing_now();
    int mix_ok = mix_images(raw_info,
                            fullres,
                            fullres_smooth,
                            halfres,
                            halfres_smooth,
                            alias_map,
                            dark_for_mix,
                            bright_for_mix,
                            overexposed,
                            dark_noise,
                            white_darkened,
                            corr_ev,
                            lowiso_dr,
                            black,
                            white,
                            0,
                            scratch);
    g_dualiso_full20bit_timing.mix_ms += dualiso_debug_elapsed_ms(stage_start);

    if (mix_ok && effective_chroma_smooth_method && use_fullres)
    {
        fullres_smooth = scratch->fullres_smooth;
        stage_start = mlv_stage_timing_now();
        fullres_reconstruction(raw_info,
                               fullres_smooth,
                               dark_for_mix,
                               bright_for_mix,
                               white_darkened,
                               is_bright);
        g_dualiso_full20bit_timing.mix_chroma_fullres_ms +=
            dualiso_debug_elapsed_ms(stage_start);
    }

    int final_blend_fused_to_16bit = 0;
    if (mix_ok)
    {
        /* let's check the ideal noise levels (on the halfres image, which in black areas is identical to the bright one) */
#ifndef STDOUT_SILENT
        //#pragma omp parallel for collapse(2)
        for (int y = 3; y < h-2; y ++)
            for (int x = 2; x < w-2; x ++)
                raw_set_pixel32(x, y, bright_for_mix[x + y*w]);
        
        compute_black_noise(raw_info, image_data, 8, raw_info.active_area.x1 - 8, raw_info.active_area.y1 + 20, raw_info.active_area.y2 - 20, 1, 1, &noise_avg, &noise_std[0]);
        double ideal_noise_std = noise_std[0];
#endif
        stage_start = mlv_stage_timing_now();
        const int final_blend_result = final_blend(raw_info,
                                                    image_data,
                                                    raw_buffer_32,
                                                    fullres,
                                                    fullres_smooth,
                                                    halfres_smooth,
                                                    dark,
                                                    bright,
                                                    overexposed,
                                                    alias_map,
                                                    black,
                                                    white,
                                                    dark_noise,
                                                    randn05_cache,
                                                    fullres_mesh_guard,
                                                    scratch);
        g_dualiso_full20bit_timing.final_blend_ms += dualiso_debug_elapsed_ms(stage_start);
        if (final_blend_result < 0)
        {
            DUALISO_FULL20_RETURN(0);
        }
        final_blend_fused_to_16bit = final_blend_result;
        if (!final_blend_fused_to_16bit)
        {
            if (fullres_mesh_guard)
            {
                int * output_raw2ev = NULL;
                float * output_raw2ev_float = NULL;
                int * output_ev2raw = NULL;
                stage_start = mlv_stage_timing_now();
                if (!ensure_scratch_ev_lut(scratch, black, white, &output_raw2ev, &output_raw2ev_float, &output_ev2raw))
                {
                    DUALISO_FULL20_RETURN(0);
                }
                (void)output_raw2ev_float;
                dualiso_output_vertical_mesh_stabilize(raw_info,
                                                       raw_buffer_32,
                                                       fullres,
                                                       output_raw2ev,
                                                       output_ev2raw);
                dualiso_source_mesh_stabilize(raw_info,
                                              raw_buffer_32,
                                              fullres,
                                              output_raw2ev,
                                              output_ev2raw);
                dualiso_output_flat_green_cell_balance(raw_info,
                                                       raw_buffer_32,
                                                       output_raw2ev,
                                                       output_ev2raw);
                dualiso_output_flat_chroma_guard(raw_info,
                                                 raw_buffer_32,
                                                 output_raw2ev,
                                                 output_ev2raw);
                g_dualiso_full20bit_timing.final_blend_ms += dualiso_debug_elapsed_ms(stage_start);
            }

            stage_start = mlv_stage_timing_now();
            convert_20_to_16bit(raw_info, image_data, raw_buffer_32);
            g_dualiso_full20bit_timing.convert16_ms += dualiso_debug_elapsed_ms(stage_start);
        }

        /* let's see how much dynamic range we actually got */
#ifndef STDOUT_SILENT
        compute_black_noise(raw_info, image_data, 8, raw_info.active_area.x1 - 8, raw_info.active_area.y1 + 20, raw_info.active_area.y2 - 20, 1, 1, &noise_avg, &noise_std[0]);
        printf("Noise level     : %.02f (20-bit), ideally %.02f\n", noise_std[0], ideal_noise_std);
        printf("Dynamic range   : %.02f EV (cooked)\n", log2(white - black) - log2(noise_std[0]));
#endif
        ret = 1;
    }

    if (ret)
    {
        *auto_correction = staged_auto_correction;
        *ev_correction = staged_ev_correction;
        *black_delta = staged_black_delta;
        restore_iso_pattern_on_failure = 0;
    }
    else if (restore_iso_pattern_on_failure)
    {
        *iso_pattern = initial_iso_pattern;
    }

    /* P3 diagnostic (gated, one-shot per process): dump the EXACT scalars and
     * curve checksums diso_get_full20bit used at point-of-use. Compared offline
     * against the published scalars.txt + mix_curve.f64/fullres_curve.f64 that
     * the backend reconstructs from, this pins any recon state-capture desync to
     * a specific field instead of guessing. Gated on the same env var as the
     * live-vector dump so it only fires in the P3 validator. */
    if(g_dualiso_gpu_recon_state_capture_enabled && ret)
    {
        static int recon_pou_dumped = 0;
        const char * pou_dir = getenv("MLVAPP_GPU_PLAYBACK_RECON_DUMP_LIVE_VECTOR_DIR");
        if(!recon_pou_dumped && pou_dir && *pou_dir && strcmp(pou_dir, "0") != 0)
        {
            const double overlap_at_use = g_dualiso_full20bit_timing.mix_curve_overlap;
            const double * mix_curve_at_use =
                dualiso_select_gpu_mix_curve(scratch,
                                             (uint32_t)black,
                                             (uint32_t)white,
                                             corr_ev,
                                             overlap_at_use);
            const double * fullres_curve_at_use =
                build_fullres_curve_float_as_double(scratch, black);
            char pou_path[1200];
            snprintf(pou_path, sizeof(pou_path), "%s/recon_pou.txt", pou_dir);
            FILE * pou_f = fopen(pou_path, "wb");
            if(pou_f)
            {
                fprintf(pou_f, "expo_matched=%d\n", expo_matched);
                fprintf(pou_f, "black=%d\n", black);
                fprintf(pou_f, "white=%d\n", white);
                fprintf(pou_f, "white_darkened=%d\n", white_darkened);
                fprintf(pou_f, "corr_ev=%.17g\n", corr_ev);
                fprintf(pou_f, "ev_correction_ptr=%.17g\n", *ev_correction);
                fprintf(pou_f, "black_delta_ptr=%d\n", *black_delta);
                fprintf(pou_f, "black_delta20_published=%d\n", (*black_delta) * 64);
                fprintf(pou_f, "dark_noise=%.17g\n", dark_noise);
                fprintf(pou_f, "dark_noise_int_round=%d\n", (int)(dark_noise + 0.5));
                fprintf(pou_f, "dark_noise_int_trunc=%d\n", (int)dark_noise);
                fprintf(pou_f, "final_blend_fused_to_16bit=%d\n", final_blend_fused_to_16bit);
                fprintf(pou_f, "interp_method=%d\n", interp_method);
                fprintf(pou_f, "use_alias_map=%d\n", use_alias_map);
                fprintf(pou_f, "use_fullres=%d\n", use_fullres);
                fprintf(pou_f, "effective_chroma_smooth_method=%d\n", effective_chroma_smooth_method);
                fprintf(pou_f, "mix_curve_overlap_at_use=%.17g\n", overlap_at_use);
                fprintf(pou_f, "mix_curve_corr_ev_at_use=%.17g\n", g_dualiso_full20bit_timing.mix_curve_corr_ev);
                fprintf(pou_f, "mix_curve_rebuilt=%d\n", g_dualiso_full20bit_timing.mix_curve_rebuilt);
                fprintf(pou_f, "mix_curve_ptr_nonnull=%d\n", mix_curve_at_use != NULL);
                fprintf(pou_f, "is_bright=%d%d%d%d\n",
                        is_bright[0], is_bright[1], is_bright[2], is_bright[3]);
                fprintf(pou_f, "mix_curve_checksum=%llu\n",
                        (unsigned long long)dualiso_p3_curve_checksum_f64(
                            mix_curve_at_use, (size_t)(1u << 20)));
                fprintf(pou_f, "fullres_curve_checksum=%llu\n",
                        (unsigned long long)dualiso_p3_curve_checksum_f64(
                            fullres_curve_at_use, (size_t)(1u << 20)));
                fprintf(pou_f, "raw2ev_checksum=%llu\n",
                        (unsigned long long)dualiso_p3_curve_checksum_i32(
                            scratch ? scratch->ev_raw2ev : NULL, (size_t)(1u << 20)));
                fprintf(pou_f, "ev2raw_checksum=%llu\n",
                        (unsigned long long)dualiso_p3_curve_checksum_i32(
                            scratch ? scratch->ev2raw_0 : NULL, (size_t)(24u * EV_RESOLUTION)));
                fclose(pou_f);
                recon_pou_dumped = 1;
            }
        }
    }

    dualiso_debug_publish_gpu_recon_state(ret,
                                          rggb,
                                          raw_info,
                                          (uint32_t)black,
                                          (uint32_t)white,
                                          (uint32_t)white_darkened,
                                          is_bright,
                                          corr_ev,
                                          *black_delta,
                                          dark_noise,
                                          interp_method,
                                          use_alias_map,
                                          use_fullres,
                                          effective_chroma_smooth_method,
                                          playback_preview_scale_factor,
                                          final_blend_fused_to_16bit,
                                          scratch);

    if (!rggb) /* back to GBRG */
    {
        raw_info.active_area.y1--;
        raw_info.active_area.y2++;
        raw_info.height++;
        h++;
    }
    
    dualiso_debug_finish_full20bit_timing(full20_start);
#undef DUALISO_FULL20_RETURN
    return ret;
}
