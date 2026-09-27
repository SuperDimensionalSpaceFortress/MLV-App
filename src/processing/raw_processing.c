#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <float.h>
#include <pthread.h>
#include <ctype.h>
#include <omp.h>
#include "blur_threaded.h"
#include "tinyexpr/tinyexpr.h"
#if defined(__linux) || defined(__APPLE__)
#include <locale.h>
#endif

#ifdef __SSE2__
  #include <emmintrin.h>
#endif

#include "raw_processing.h"
#include "../mlv/video_mlv.h"
#include "../mlv/pipeline_stage_capture.h"
#include "filter/filter.h"
#include "denoiser/denoiser_2d_median.h"
#include "../mlv/camid/camera_id.h"
#include "interpolation/spline_helper.h"
#include "interpolation/cosine_interpolation.h"
#include "rbfilter/rbf_wrapper.h"
#include "sobel/sobel.h"
#include "cafilter/ColorAberrationCorrection.h"

/* Matrix functions which are useful */
#include "../matrix/matrix.h"

#define STANDARD_GAMMA 3.15

#define MIN(X, Y) (((X) < (Y)) ? (X) : (Y))
#define MAX(X, Y) (((X) > (Y)) ? (X) : (Y))
#define LIMIT16(X) MAX(MIN(X, 65535), 0)

#if defined(_MSC_VER)
#define MLV_PROCESSING_THREAD_LOCAL __declspec(thread)
#else
#define MLV_PROCESSING_THREAD_LOCAL __thread
#endif

static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_setup_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_prep_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_resize_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_copy_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_fullres_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_halfres_downsample_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_halfres_rbf_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_halfres_upsample_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_quarterres_downsample_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_quarterres_rbf_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_filter_quarterres_upsample_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_total_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_boundary_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_range_table_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_left_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_right_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_horizontal_average_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_down_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_first_line_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_diff_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_factor_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_src_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_prev_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_assign_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_vertical_up_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_shadows_highlights_rbf_output_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_highest_green_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_denoise_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_rbf_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_ca_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_chroma_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_sharpen_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_grain_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_levels_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_gradient_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_vignette_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_creative_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_creative_shadows_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_creative_contrast_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_matrix_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_matrix_r_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_matrix_g_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_matrix_b_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_exposure_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_gamut_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_main_prelude_wb_recon_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_main_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_gradient_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_wb_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_wb_matrix_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_wb_gamut_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_wb_desat_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_clip_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_clip_neg_r_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_clip_neg_g_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_clip_neg_b_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_r_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_g_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_b_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_r_hi_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_g_hi_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_cam_agx_matrix_b_hi_count = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_gamma_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_gamma_main_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_color_gamma_gradient_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_hue_vs_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_vibrance_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_saturation_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_toning_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_curve_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_gradation_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_creative_agx_inverse_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_core_output_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_direct8_matrix_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_direct8_gamma_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL double g_processing_last_direct8_curves_ms = 0.0;
static MLV_PROCESSING_THREAD_LOCAL char g_processing_last_direct8_incompatibility_reason[128] = "none";
static MLV_PROCESSING_THREAD_LOCAL int g_processing_playback_preview_mode = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_playback_aggressive_preview_mode = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_playback_preview_scale_factor = 1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_quarterres_path_taken = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_quarterres_downsample_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_quarterres_rbf_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_quarterres_upsample_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_halfres_downsample_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_halfres_rbf_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_halfres_upsample_completed = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_fullres_completed = 0;
/* Monotonic per-call counters proving the SH blur pre-pass / filter actually
 * executed. A wall-clock Milliseconds() field can legitimately read 0.0 when
 * the stage runs faster than the timer resolves, so it must never be the
 * only evidence that a stage ran (see PROD-TELEMETRY-DURATION-AS-PROOF-1). */
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_prep_run_count = 0;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_last_shadows_highlights_filter_run_count = 0;

static int g_processing_shadows_highlights_probe_initialized = 0;
static int g_processing_shadows_highlights_probe_mode = -1;
/* THREAD-LOCAL since 2026-06-11: these cache a policy derived from the
 * THREAD-LOCAL playback preview flags. As shared statics, a background
 * render thread (the processed8 prefetch worker now runs the full 16-bit
 * pipeline) could recompute them under ITS flag state and poison the
 * foreground's answer whenever the two threads' modes differ. */
static MLV_PROCESSING_THREAD_LOCAL int g_processing_standard_x1_shadows_highlights_quarterres_cache = -1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_standard_x2_shadows_highlights_quarterres_cache = -1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_standard_x4_shadows_highlights_quarterres_cache = -1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_aggressive_x2_shadows_highlights_quarterres_cache = -1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_aggressive_x8_shadows_highlights_quarterres_cache = -1;
static MLV_PROCESSING_THREAD_LOCAL int g_processing_direct8_preview_quarterres_sh_cache = -1;

static int processing_env_flag_enabled(const char * value)
{
    if( !value || !*value ) return 0;
    if( strcmp(value, "1") == 0 ) return 1;
    if( strcmp(value, "true") == 0 ) return 1;
    if( strcmp(value, "TRUE") == 0 ) return 1;
    if( strcmp(value, "True") == 0 ) return 1;
    if( strcmp(value, "yes") == 0 ) return 1;
    if( strcmp(value, "on") == 0 ) return 1;
    return 0;
}

static int processing_parse_probe_mode(const char * value, int max_mode)
{
    if( value && *value )
    {
        char * end = NULL;
        long parsed = strtol(value, &end, 10);
        if( end != value && *end == '\0' && parsed >= 0 && parsed <= max_mode )
        {
            return (int)parsed;
        }
    }
    return -1;
}

static int processing_shadows_highlights_curve_index_mask_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if( !initialized )
    {
        /* Experimental A/B path: parity-safe, but GUI playback timings were
         * mixed, so keep default playback on the established RGB blur mask. */
        enabled =
            processing_env_flag_enabled(getenv("MLVAPP_ENABLE_SH_CURVE_INDEX_MASK"))
            && !processing_env_flag_enabled(getenv("MLVAPP_DISABLE_SH_CURVE_INDEX_MASK"));
        initialized = 1;
    }
    return enabled;
}

static int processing_shadows_highlights_probe_mode(void)
{
    if( !g_processing_shadows_highlights_probe_initialized )
    {
        g_processing_shadows_highlights_probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE"), 1);
        g_processing_shadows_highlights_probe_initialized = 1;
    }
    return g_processing_shadows_highlights_probe_mode;
}

static int processing_aggressive_x8_shadows_highlights_quarterres_enabled(void)
{
    if( g_processing_aggressive_x8_shadows_highlights_quarterres_cache < 0 )
    {
        g_processing_aggressive_x8_shadows_highlights_quarterres_cache = processing_env_flag_enabled(
            getenv("MLVAPP_DISABLE_AGGRESSIVE_X8_SH_QUARTERRES") ) ? 0 : 1;
    }
    return g_processing_aggressive_x8_shadows_highlights_quarterres_cache;
}

static int processing_aggressive_x2_shadows_highlights_quarterres_enabled(void)
{
    if( g_processing_aggressive_x2_shadows_highlights_quarterres_cache < 0 )
    {
        g_processing_aggressive_x2_shadows_highlights_quarterres_cache = processing_env_flag_enabled(
            getenv("MLVAPP_DISABLE_AGGRESSIVE_X2_SH_QUARTERRES") ) ? 0 : 1;
    }
    return g_processing_aggressive_x2_shadows_highlights_quarterres_cache;
}

/* Round-4 item 6: let the direct8 PLAYBACK lanes use the cheap preview
 * shadows/highlights blur (quarter-res RBF) instead of the export-policy
 * half/full-res RBF. The SH blur is a smooth low-frequency mask, so a
 * quarter-res RBF upsampled to full dims is visually equivalent to the
 * half-res one (the standard preview lanes already ship this), while cutting
 * the ~21ms full-quality-x1 SH-prep ~4x. applyProcessingObject8 used
 * force_export_policy=1 unconditionally as a round-4 item 2 band-aid; the
 * corruption that motivated it was the TLS scaled-input self-alias (fixed
 * separately, same item), NOT the blur resolution - the blur_image is full
 * imageX*imageY in every branch, so the kernel reads identical dims. EXPORTS
 * (preview mode OFF) keep the export policy and stay full quality. Kill
 * switch restores the band-aid for A/B and safety. */
static int processing_direct8_preview_quarterres_sh_enabled(void)
{
    if( g_processing_direct8_preview_quarterres_sh_cache < 0 )
    {
        g_processing_direct8_preview_quarterres_sh_cache = processing_env_flag_enabled(
            getenv("MLVAPP_DISABLE_DIRECT8_PREVIEW_QUARTERRES_SH") ) ? 0 : 1;
    }
    return g_processing_direct8_preview_quarterres_sh_cache;
}

static int processing_standard_x1_shadows_highlights_quarterres_enabled(void)
{
    static MLV_PROCESSING_THREAD_LOCAL int cached_preview_mode_enabled = -1;
    static MLV_PROCESSING_THREAD_LOCAL int cached_aggressive_preview_enabled = -1;
    const int preview_mode_enabled = processingPlaybackPreviewModeEnabled();
    const int aggressive_preview_enabled = preview_mode_enabled
        && processingPlaybackAggressivePreviewModeEnabled();
    if( g_processing_standard_x1_shadows_highlights_quarterres_cache < 0
     || cached_preview_mode_enabled != preview_mode_enabled
     || cached_aggressive_preview_enabled != aggressive_preview_enabled )
    {
        const int disabled =
            processing_env_flag_enabled(getenv("MLVAPP_DISABLE_STANDARD_X1_SH_QUARTERRES"));
        g_processing_standard_x1_shadows_highlights_quarterres_cache =
            !disabled
         && (
                (preview_mode_enabled && !aggressive_preview_enabled)
             || processing_env_flag_enabled(getenv("MLVAPP_ENABLE_STANDARD_X1_SH_QUARTERRES"))
            );
        cached_preview_mode_enabled = preview_mode_enabled;
        cached_aggressive_preview_enabled = aggressive_preview_enabled;
    }
    return g_processing_standard_x1_shadows_highlights_quarterres_cache;
}

static int processing_standard_x2_shadows_highlights_quarterres_enabled(void)
{
    static MLV_PROCESSING_THREAD_LOCAL int cached_preview_mode_enabled = -1;
    static MLV_PROCESSING_THREAD_LOCAL int cached_aggressive_preview_enabled = -1;
    const int preview_mode_enabled = processingPlaybackPreviewModeEnabled();
    const int aggressive_preview_enabled = preview_mode_enabled
        && processingPlaybackAggressivePreviewModeEnabled();
    if( g_processing_standard_x2_shadows_highlights_quarterres_cache < 0
     || cached_preview_mode_enabled != preview_mode_enabled
     || cached_aggressive_preview_enabled != aggressive_preview_enabled )
    {
        const int disabled =
            processing_env_flag_enabled(getenv("MLVAPP_DISABLE_STANDARD_X2_SH_QUARTERRES"));
        g_processing_standard_x2_shadows_highlights_quarterres_cache =
            !disabled
         && (
                (preview_mode_enabled && !aggressive_preview_enabled)
             || processing_env_flag_enabled(getenv("MLVAPP_ENABLE_STANDARD_X2_SH_QUARTERRES"))
            );
        cached_preview_mode_enabled = preview_mode_enabled;
        cached_aggressive_preview_enabled = aggressive_preview_enabled;
    }
    return g_processing_standard_x2_shadows_highlights_quarterres_cache;
}

static int processing_standard_x4_shadows_highlights_quarterres_enabled(void)
{
    if( g_processing_standard_x4_shadows_highlights_quarterres_cache < 0 )
    {
        g_processing_standard_x4_shadows_highlights_quarterres_cache =
            processing_env_flag_enabled(getenv("MLVAPP_ENABLE_STANDARD_X4_SH_QUARTERRES"))
            && !processing_env_flag_enabled(getenv("MLVAPP_DISABLE_STANDARD_X4_SH_QUARTERRES"));
    }
    return g_processing_standard_x4_shadows_highlights_quarterres_cache;
}

static int processing_main_prelude_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_PROBE"), 4);
        initialized = 1;
    }
    return probe_mode;
}

static int processing_main_prelude_wb_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE"), 3);
        initialized = 1;
    }
    return probe_mode;
}

static int processing_main_prelude_creative_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_CREATIVE_PROBE"), 4);
        initialized = 1;
    }
    return probe_mode;
}

static int processing_cam_wb_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_COLOR_CAM_WB_PROBE"), 7);
        initialized = 1;
    }
    return probe_mode;
}

static int processing_cam_gamma_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_COLOR_GAMMA_PROBE"), 2);
        initialized = 1;
    }
    return probe_mode;
}

static int processing_creative_probe_mode(void)
{
    static int initialized = 0;
    static int probe_mode = -1;
    if( !initialized )
    {
        probe_mode = processing_parse_probe_mode(
            getenv("MLVAPP_PROCESSING_CORE_CREATIVE_PROBE"), 7);
        initialized = 1;
    }
    return probe_mode;
}

typedef struct
{
    double matrix_ms;
    double gamma_ms;
    double curves_ms;
} processing_direct8_kernel_timing_t;

static void processing_capture_last_shadows_highlights_rbf_timing(void);

void processingResetLastTimingTelemetry(void)
{
    g_processing_last_setup_ms = 0.0;
    g_processing_last_shadows_highlights_prep_ms = 0.0;
    g_processing_last_shadows_highlights_resize_ms = 0.0;
    g_processing_last_shadows_highlights_copy_ms = 0.0;
    g_processing_last_shadows_highlights_filter_ms = 0.0;
    g_processing_last_shadows_highlights_filter_fullres_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_downsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_rbf_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_upsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_downsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_rbf_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_upsample_ms = 0.0;
    g_processing_last_shadows_highlights_quarterres_downsample_completed = 0;
    g_processing_last_shadows_highlights_quarterres_rbf_completed = 0;
    g_processing_last_shadows_highlights_quarterres_upsample_completed = 0;
    g_processing_last_shadows_highlights_halfres_downsample_completed = 0;
    g_processing_last_shadows_highlights_halfres_rbf_completed = 0;
    g_processing_last_shadows_highlights_halfres_upsample_completed = 0;
    g_processing_last_shadows_highlights_fullres_completed = 0;
    g_processing_last_shadows_highlights_prep_run_count = 0;
    g_processing_last_shadows_highlights_filter_run_count = 0;
    g_processing_last_shadows_highlights_rbf_total_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_boundary_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_range_table_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_left_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_right_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_horizontal_average_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_down_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_first_line_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_diff_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_factor_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_src_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_prev_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_assign_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_output_ms = 0.0;
    g_processing_last_highest_green_ms = 0.0;
    g_processing_last_core_ms = 0.0;
    g_processing_last_denoise_ms = 0.0;
    g_processing_last_rbf_ms = 0.0;
    g_processing_last_ca_ms = 0.0;
    g_processing_last_chroma_ms = 0.0;
    g_processing_last_sharpen_ms = 0.0;
    g_processing_last_grain_ms = 0.0;
    g_processing_last_core_levels_ms = 0.0;
    g_processing_last_core_color_ms = 0.0;
    g_processing_last_core_color_main_ms = 0.0;
    g_processing_last_core_color_gradient_ms = 0.0;
    g_processing_last_core_color_main_prelude_ms = 0.0;
    g_processing_last_core_color_main_prelude_vignette_ms = 0.0;
    g_processing_last_core_color_main_prelude_creative_ms = 0.0;
    g_processing_last_core_color_main_prelude_creative_shadows_ms = 0.0;
    g_processing_last_core_color_main_prelude_creative_contrast_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_matrix_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_matrix_r_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_matrix_g_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_matrix_b_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_exposure_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_gamut_ms = 0.0;
    g_processing_last_core_color_main_prelude_wb_recon_ms = 0.0;
    g_processing_last_core_color_cam_ms = 0.0;
    g_processing_last_core_color_cam_main_ms = 0.0;
    g_processing_last_core_color_cam_gradient_ms = 0.0;
    g_processing_last_core_color_cam_wb_ms = 0.0;
    g_processing_last_core_color_cam_wb_matrix_ms = 0.0;
    g_processing_last_core_color_cam_wb_gamut_ms = 0.0;
    g_processing_last_core_color_cam_wb_desat_ms = 0.0;
    g_processing_last_core_color_cam_agx_ms = 0.0;
    g_processing_last_core_color_cam_agx_clip_ms = 0.0;
    g_processing_last_core_color_cam_agx_clip_neg_r_count = 0.0;
    g_processing_last_core_color_cam_agx_clip_neg_g_count = 0.0;
    g_processing_last_core_color_cam_agx_clip_neg_b_count = 0.0;
    g_processing_last_core_color_cam_agx_matrix_ms = 0.0;
    g_processing_last_core_color_cam_agx_matrix_r_ms = 0.0;
    g_processing_last_core_color_cam_agx_matrix_g_ms = 0.0;
    g_processing_last_core_color_cam_agx_matrix_b_ms = 0.0;
    g_processing_last_core_color_cam_agx_matrix_r_hi_count = 0.0;
    g_processing_last_core_color_cam_agx_matrix_g_hi_count = 0.0;
    g_processing_last_core_color_cam_agx_matrix_b_hi_count = 0.0;
    g_processing_last_core_color_gamma_ms = 0.0;
    g_processing_last_core_color_gamma_main_ms = 0.0;
    g_processing_last_core_color_gamma_gradient_ms = 0.0;
    g_processing_last_core_creative_ms = 0.0;
    g_processing_last_core_creative_hue_vs_ms = 0.0;
    g_processing_last_core_creative_vibrance_ms = 0.0;
    g_processing_last_core_creative_saturation_ms = 0.0;
    g_processing_last_core_creative_toning_ms = 0.0;
    g_processing_last_core_creative_curve_ms = 0.0;
    g_processing_last_core_creative_gradation_ms = 0.0;
    g_processing_last_core_creative_agx_inverse_ms = 0.0;
    g_processing_last_core_output_ms = 0.0;
    g_processing_last_direct8_matrix_ms = 0.0;
    g_processing_last_direct8_gamma_ms = 0.0;
    g_processing_last_direct8_curves_ms = 0.0;
}

void processingSetPlaybackPreviewMode(int enabled)
{
    const int normalized = enabled ? 1 : 0;
    if (processingPlaybackPreviewModeEnabled() == normalized)
    {
        return;
    }
    g_processing_playback_preview_mode = normalized;
}

int processingPlaybackPreviewModeEnabled(void)
{
    return g_processing_playback_preview_mode;
}

void processingSetPlaybackAggressivePreviewMode(int enabled)
{
    const int normalized = enabled ? 1 : 0;
    if (processingPlaybackAggressivePreviewModeEnabled() == normalized)
    {
        return;
    }
    g_processing_playback_aggressive_preview_mode = normalized;
}

int processingPlaybackAggressivePreviewModeEnabled(void)
{
    return g_processing_playback_aggressive_preview_mode;
}

void processingSetPlaybackPreviewScaleFactor(int scaleFactor)
{
    const int normalized = (scaleFactor > 1) ? scaleFactor : 1;
    if (processingPlaybackPreviewScaleFactor() == normalized)
    {
        return;
    }
    g_processing_playback_preview_scale_factor = normalized;
}

int processingPlaybackPreviewScaleFactor(void)
{
    return g_processing_playback_preview_scale_factor;
}

void processingResetShadowsHighlightsProbeModeCacheForTesting(void)
{
    g_processing_shadows_highlights_probe_initialized = 0;
    g_processing_shadows_highlights_probe_mode = -1;
}

void processingResetShadowsHighlightsQuarterresEnvCacheForTesting(void)
{
    g_processing_standard_x1_shadows_highlights_quarterres_cache = -1;
    g_processing_standard_x2_shadows_highlights_quarterres_cache = -1;
    g_processing_standard_x4_shadows_highlights_quarterres_cache = -1;
    g_processing_aggressive_x2_shadows_highlights_quarterres_cache = -1;
    g_processing_aggressive_x8_shadows_highlights_quarterres_cache = -1;
    g_processing_direct8_preview_quarterres_sh_cache = -1;
}

static void processing_core_timing_reset(processing_core_timing_t * timing)
{
    if( !timing ) return;

    timing->levels_ms = 0.0;
    timing->color_ms = 0.0;
    timing->color_main_ms = 0.0;
    timing->color_gradient_ms = 0.0;
    timing->color_main_prelude_ms = 0.0;
    timing->color_main_prelude_vignette_ms = 0.0;
    timing->color_main_prelude_creative_ms = 0.0;
    timing->color_main_prelude_creative_shadows_ms = 0.0;
    timing->color_main_prelude_creative_contrast_ms = 0.0;
    timing->color_main_prelude_wb_ms = 0.0;
    timing->color_main_prelude_wb_matrix_ms = 0.0;
    timing->color_main_prelude_wb_matrix_r_ms = 0.0;
    timing->color_main_prelude_wb_matrix_g_ms = 0.0;
    timing->color_main_prelude_wb_matrix_b_ms = 0.0;
    timing->color_main_prelude_wb_gradient_matrix_ms = 0.0;
    timing->color_main_prelude_wb_exposure_ms = 0.0;
    timing->color_main_prelude_wb_gamut_ms = 0.0;
    timing->color_main_prelude_wb_recon_ms = 0.0;
    timing->color_cam_ms = 0.0;
    timing->color_cam_main_ms = 0.0;
    timing->color_cam_gradient_ms = 0.0;
    timing->color_cam_wb_ms = 0.0;
    timing->color_cam_wb_matrix_ms = 0.0;
    timing->color_cam_wb_gamut_ms = 0.0;
    timing->color_cam_wb_desat_ms = 0.0;
    timing->color_cam_agx_ms = 0.0;
    timing->color_cam_agx_clip_ms = 0.0;
    timing->color_cam_agx_clip_neg_r_count = 0.0;
    timing->color_cam_agx_clip_neg_g_count = 0.0;
    timing->color_cam_agx_clip_neg_b_count = 0.0;
    timing->color_cam_agx_matrix_ms = 0.0;
    timing->color_cam_agx_matrix_r_ms = 0.0;
    timing->color_cam_agx_matrix_g_ms = 0.0;
    timing->color_cam_agx_matrix_b_ms = 0.0;
    timing->color_cam_agx_matrix_r_hi_count = 0.0;
    timing->color_cam_agx_matrix_g_hi_count = 0.0;
    timing->color_cam_agx_matrix_b_hi_count = 0.0;
    timing->color_gamma_ms = 0.0;
    timing->color_gamma_main_ms = 0.0;
    timing->color_gamma_gradient_ms = 0.0;
    timing->creative_ms = 0.0;
    timing->creative_hue_vs_ms = 0.0;
    timing->creative_vibrance_ms = 0.0;
    timing->creative_saturation_ms = 0.0;
    timing->creative_toning_ms = 0.0;
    timing->creative_curve_ms = 0.0;
    timing->creative_gradation_ms = 0.0;
    timing->creative_agx_inverse_ms = 0.0;
    timing->output_ms = 0.0;
}

/* Thank you to https://gist.github.com/MrLixm/946c1b59cce8b74e948e75618583ce8d */
double agx_compressed_matrix[9] = {
    0.84247906, 0.0784336, 0.07922375,
    0.04232824, 0.87846864, 0.07916613,
    0.04237565, 0.0784336, 0.87914297
};

static inline uint16_t agx_store_u16_fast(const double value)
{
    if( value <= 65535.0 )
    {
        return (uint16_t)value;
    }
    return (uint16_t)LIMIT16(value);
}

static inline void agx_store_u16_triplet_fast(const double out_r,
                                              const double out_g,
                                              const double out_b,
                                              uint16_t * const pix0,
                                              uint16_t * const pix1,
                                              uint16_t * const pix2)
{
    if( out_r >= 0.0 && out_r <= 65535.0 &&
        out_g >= 0.0 && out_g <= 65535.0 &&
        out_b >= 0.0 && out_b <= 65535.0 )
    {
        *pix0 = (uint16_t)out_r;
        *pix1 = (uint16_t)out_g;
        *pix2 = (uint16_t)out_b;
    }
    else
    {
        *pix0 = (uint16_t)LIMIT16(out_r);
        *pix1 = (uint16_t)LIMIT16(out_g);
        *pix2 = (uint16_t)LIMIT16(out_b);
    }
}

static inline void agx_store_float_triplet_fast(const double out_r,
                                                const double out_g,
                                                const double out_b,
                                                float * const pix0,
                                                float * const pix1,
                                                float * const pix2)
{
    /* The gradient layer remains float until its LUT stage.  Keep its typed
     * storage separate from the uint16_t output-pixel fast path while
     * preserving the original per-channel 16-bit quantization for valid
     * values. Clamp before converting so malformed negative/NaN inputs are
     * defined instead of reaching an out-of-range float-to-integer cast. */
    *pix0 = (float)(uint16_t)LIMIT16(out_r);
    *pix1 = (float)(uint16_t)LIMIT16(out_g);
    *pix2 = (float)(uint16_t)LIMIT16(out_b);
}

#ifndef __APPLE__
#define M_PI 3.14159265358979323846 /* pi */
#endif

/* Because why compile a whole .o just for this? */
#include "processing.c"
/* Default image profiles */
#include "image_profiles.c"
/* White balance cr4p */
#include "white_balance.c"

#if defined(__linux)
#include <alloca.h>
#endif

/* Easy way to deal with storing blurred images/stuff that takes ages to calculate */
processing_buffer_t * new_image_buffer()
{
    processing_buffer_t * buffer = (processing_buffer_t *)calloc(sizeof(processing_buffer_t),1);
    buffer->image = NULL;
    return buffer;
}
void buffer_set_size(processing_buffer_t * buffer, int width, int height)
{
    /* Only if it is needed */
    if (buffer->width != width || buffer->height != height)
    {
        buffer->width = width;
        buffer->height = height;
        if (buffer->image != NULL) free(buffer->image);
        buffer->image = malloc(sizeof(uint16_t) * 3 * width * height);
    }
}
uint16_t * get_buffer(processing_buffer_t * buffer)
{
    return buffer->image;
}
void free_image_buffer(processing_buffer_t * buffer)
{
    if (buffer->image != NULL) free(buffer->image);
    free(buffer);
}

static int ensure_processing_u16_scratch(uint16_t ** buffer, size_t * capacity, size_t required)
{
    if( !buffer || !capacity ) return 0;
    if( *capacity >= required && *buffer ) return 1;

    uint16_t * resized = realloc(*buffer, required * sizeof(uint16_t));
    if( !resized ) return 0;

    *buffer = resized;
    *capacity = required;
    return 1;
}

static void rgb_u16_downsample_2x_box(const uint16_t * __restrict src,
                                      uint16_t * __restrict dst,
                                      int src_w,
                                      int src_h,
                                      int threads)
{
    const int dst_w = src_w >> 1;
    const int dst_h = src_h >> 1;
    const size_t src_stride = (size_t)src_w * 3u;
    const size_t dst_stride = (size_t)dst_w * 3u;

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < dst_h; ++y)
    {
        const uint16_t * row0 = src + (size_t)(y * 2) * src_stride;
        const uint16_t * row1 = row0 + src_stride;
        uint16_t * drow = dst + (size_t)y * dst_stride;
        for (int x = 0; x < dst_w; ++x)
        {
            const uint16_t * p00 = row0 + (size_t)(x * 2) * 3u;
            const uint16_t * p01 = p00 + 3;
            const uint16_t * p10 = row1 + (size_t)(x * 2) * 3u;
            const uint16_t * p11 = p10 + 3;
            uint16_t * out = drow + (size_t)x * 3u;
            out[0] = (uint16_t)(((uint32_t)p00[0] + (uint32_t)p01[0]
                                + (uint32_t)p10[0] + (uint32_t)p11[0]) >> 2);
            out[1] = (uint16_t)(((uint32_t)p00[1] + (uint32_t)p01[1]
                                + (uint32_t)p10[1] + (uint32_t)p11[1]) >> 2);
            out[2] = (uint16_t)(((uint32_t)p00[2] + (uint32_t)p01[2]
                                + (uint32_t)p10[2] + (uint32_t)p11[2]) >> 2);
        }
    }
}

static void rgb_u16_downsample_4x_two_stage_box(const uint16_t * __restrict src,
                                                uint16_t * __restrict dst,
                                                int src_w,
                                                int src_h,
                                                int threads)
{
    const int dst_w = src_w >> 2;
    const int dst_h = src_h >> 2;
    const size_t src_stride = (size_t)src_w * 3u;
    const size_t dst_stride = (size_t)dst_w * 3u;

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < dst_h; ++y)
    {
        uint16_t * drow = dst + (size_t)y * dst_stride;
        const int sy = y << 2;
        for (int x = 0; x < dst_w; ++x)
        {
            const int sx = x << 2;
            uint16_t * out = drow + (size_t)x * 3u;
            for (int c = 0; c < 3; ++c)
            {
                uint32_t half_sum = 0;
                for (int by = 0; by < 2; ++by)
                {
                    for (int bx = 0; bx < 2; ++bx)
                    {
                        const int base_x = sx + (bx << 1);
                        const int base_y = sy + (by << 1);
                        const uint16_t * p00 =
                            src + (size_t)base_y * src_stride + (size_t)base_x * 3u + c;
                        const uint16_t * p01 = p00 + 3;
                        const uint16_t * p10 = p00 + src_stride;
                        const uint16_t * p11 = p10 + 3;
                        half_sum += ((uint32_t)p00[0] + (uint32_t)p01[0]
                                   + (uint32_t)p10[0] + (uint32_t)p11[0]) >> 2;
                    }
                }
                out[c] = (uint16_t)(half_sum >> 2);
            }
        }
    }
}

static void rgb_u16_upsample_2x_bilinear(const uint16_t * __restrict src,
                                         uint16_t * __restrict dst,
                                         int src_w,
                                         int src_h,
                                         int threads)
{
    const int dst_w = src_w << 1;
    const size_t src_stride = (size_t)src_w * 3u;
    const size_t dst_stride = (size_t)dst_w * 3u;

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y0 = 0; y0 < src_h; ++y0)
    {
        const int y1 = (y0 + 1 < src_h) ? (y0 + 1) : y0;
        const uint16_t * row0 = src + (size_t)y0 * src_stride;
        const uint16_t * row1 = src + (size_t)y1 * src_stride;
        uint16_t * even_row = dst + (size_t)(y0 * 2) * dst_stride;
        uint16_t * odd_row = even_row + dst_stride;

        for (int x0 = 0; x0 < src_w; ++x0)
        {
            const int x1 = (x0 + 1 < src_w) ? (x0 + 1) : x0;
            const uint16_t * p00 = row0 + (size_t)x0 * 3u;
            const uint16_t * p01 = row0 + (size_t)x1 * 3u;
            const uint16_t * p10 = row1 + (size_t)x0 * 3u;
            const uint16_t * p11 = row1 + (size_t)x1 * 3u;
            uint16_t * even = even_row + (size_t)(x0 * 2) * 3u;
            uint16_t * odd = odd_row + (size_t)(x0 * 2) * 3u;

            const uint32_t r00 = p00[0], g00 = p00[1], b00 = p00[2];
            const uint32_t r01 = p01[0], g01 = p01[1], b01 = p01[2];
            const uint32_t r10 = p10[0], g10 = p10[1], b10 = p10[2];
            const uint32_t r11 = p11[0], g11 = p11[1], b11 = p11[2];

            even[0] = (uint16_t)r00;
            even[1] = (uint16_t)g00;
            even[2] = (uint16_t)b00;
            even[3] = (uint16_t)((r00 + r01) >> 1);
            even[4] = (uint16_t)((g00 + g01) >> 1);
            even[5] = (uint16_t)((b00 + b01) >> 1);

            odd[0] = (uint16_t)((r00 + r10) >> 1);
            odd[1] = (uint16_t)((g00 + g10) >> 1);
            odd[2] = (uint16_t)((b00 + b10) >> 1);
            odd[3] = (uint16_t)((r00 + r01 + r10 + r11) >> 2);
            odd[4] = (uint16_t)((g00 + g01 + g10 + g11) >> 2);
            odd[5] = (uint16_t)((b00 + b01 + b10 + b11) >> 2);
        }
    }
}

static void rgb_u16_upsample_2x_bilinear_to_size_generic(const uint16_t * __restrict src,
                                                         uint16_t * __restrict dst,
                                                         int src_w,
                                                         int src_h,
                                                         int dst_w,
                                                         int dst_h,
                                                         int threads)
{
    if( !src || !dst || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0 )
    {
        return;
    }

    const size_t src_stride = (size_t)src_w * 3u;
    const size_t dst_stride = (size_t)dst_w * 3u;

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < dst_h; ++y)
    {
        int y0 = y >> 1;
        if( y0 >= src_h ) y0 = src_h - 1;
        const int y_odd = y & 1;
        const int y1 = (y_odd && y0 + 1 < src_h) ? (y0 + 1) : y0;
        const uint16_t * row0 = src + (size_t)y0 * src_stride;
        const uint16_t * row1 = src + (size_t)y1 * src_stride;
        uint16_t * drow = dst + (size_t)y * dst_stride;

        for (int x = 0; x < dst_w; ++x)
        {
            int x0 = x >> 1;
            if( x0 >= src_w ) x0 = src_w - 1;
            const int x_odd = x & 1;
            const int x1 = (x_odd && x0 + 1 < src_w) ? (x0 + 1) : x0;
            const uint16_t * p00 = row0 + (size_t)x0 * 3u;
            const uint16_t * p01 = row0 + (size_t)x1 * 3u;
            const uint16_t * p10 = row1 + (size_t)x0 * 3u;
            const uint16_t * p11 = row1 + (size_t)x1 * 3u;
            uint16_t * out = drow + (size_t)x * 3u;

            if( x_odd && y_odd )
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + (uint32_t)p01[0]
                                    + (uint32_t)p10[0] + (uint32_t)p11[0]) >> 2);
                out[1] = (uint16_t)(((uint32_t)p00[1] + (uint32_t)p01[1]
                                    + (uint32_t)p10[1] + (uint32_t)p11[1]) >> 2);
                out[2] = (uint16_t)(((uint32_t)p00[2] + (uint32_t)p01[2]
                                    + (uint32_t)p10[2] + (uint32_t)p11[2]) >> 2);
            }
            else if( x_odd )
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + (uint32_t)p01[0]) >> 1);
                out[1] = (uint16_t)(((uint32_t)p00[1] + (uint32_t)p01[1]) >> 1);
                out[2] = (uint16_t)(((uint32_t)p00[2] + (uint32_t)p01[2]) >> 1);
            }
            else if( y_odd )
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + (uint32_t)p10[0]) >> 1);
                out[1] = (uint16_t)(((uint32_t)p00[1] + (uint32_t)p10[1]) >> 1);
                out[2] = (uint16_t)(((uint32_t)p00[2] + (uint32_t)p10[2]) >> 1);
            }
            else
            {
                out[0] = p00[0];
                out[1] = p00[1];
                out[2] = p00[2];
            }
        }
    }
}

static void rgb_u16_upsample_2x_bilinear_to_size(const uint16_t * __restrict src,
                                                 uint16_t * __restrict dst,
                                                 int src_w,
                                                 int src_h,
                                                 int dst_w,
                                                 int dst_h,
                                                 int threads)
{
    if( !src || !dst || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0 )
    {
        return;
    }

    if( dst_w == (src_w << 1) && dst_h == (src_h << 1) )
    {
        rgb_u16_upsample_2x_bilinear(src, dst, src_w, src_h, threads);
        return;
    }

    rgb_u16_upsample_2x_bilinear_to_size_generic(src,
                                                 dst,
                                                 src_w,
                                                 src_h,
                                                 dst_w,
                                                 dst_h,
                                                 threads);
}

int processingRgbU16UpsampleExact2xMatchesGenericForTesting(const uint16_t * src,
                                                            int src_w,
                                                            int src_h,
                                                            int threads)
{
    if( !src || src_w <= 0 || src_h <= 0 )
    {
        return 0;
    }

    const int dst_w = src_w << 1;
    const int dst_h = src_h << 1;
    const size_t dst_words = (size_t)dst_w * (size_t)dst_h * 3u;
    uint16_t * exact = (uint16_t *)calloc(dst_words, sizeof(uint16_t));
    uint16_t * generic = (uint16_t *)calloc(dst_words, sizeof(uint16_t));
    if( !exact || !generic )
    {
        free(exact);
        free(generic);
        return 0;
    }

    rgb_u16_upsample_2x_bilinear(src, exact, src_w, src_h, threads);
    rgb_u16_upsample_2x_bilinear_to_size_generic(src,
                                                 generic,
                                                 src_w,
                                                 src_h,
                                                 dst_w,
                                                 dst_h,
                                                 threads);
    const int matches =
        memcmp(exact, generic, dst_words * sizeof(uint16_t)) == 0;
    free(exact);
    free(generic);
    return matches;
}

int processingRgbU16DownsampleExact4xMatchesTwoStepForTesting(const uint16_t * src,
                                                              int src_w,
                                                              int src_h,
                                                              int threads)
{
    if( !src || src_w < 4 || src_h < 4 )
    {
        return 0;
    }

    const int half_w = src_w >> 1;
    const int half_h = src_h >> 1;
    const int dst_w = half_w >> 1;
    const int dst_h = half_h >> 1;
    if( dst_w <= 0 || dst_h <= 0 )
    {
        return 0;
    }

    const size_t half_words = (size_t)half_w * (size_t)half_h * 3u;
    const size_t dst_words = (size_t)dst_w * (size_t)dst_h * 3u;
    uint16_t * half = (uint16_t *)calloc(half_words, sizeof(uint16_t));
    uint16_t * two_step = (uint16_t *)calloc(dst_words, sizeof(uint16_t));
    uint16_t * direct = (uint16_t *)calloc(dst_words, sizeof(uint16_t));
    if( !half || !two_step || !direct )
    {
        free(half);
        free(two_step);
        free(direct);
        return 0;
    }

    rgb_u16_downsample_2x_box(src, half, src_w, src_h, threads);
    rgb_u16_downsample_2x_box(half, two_step, half_w, half_h, threads);
    rgb_u16_downsample_4x_two_stage_box(src, direct, src_w, src_h, threads);

    const int matches =
        memcmp(two_step, direct, dst_words * sizeof(uint16_t)) == 0;
    free(half);
    free(two_step);
    free(direct);
    return matches;
}

static int ensure_sharpen_mask_scratch(processingObject_t * processing, size_t pixel_count)
{
    return ensure_processing_u16_scratch(&processing->sharpen_mask_gray,
                                         &processing->sharpen_mask_capacity,
                                         pixel_count)
        && ensure_processing_u16_scratch(&processing->sharpen_mask_sobel_h,
                                         &processing->sharpen_mask_capacity,
                                         pixel_count)
        && ensure_processing_u16_scratch(&processing->sharpen_mask_sobel_v,
                                         &processing->sharpen_mask_capacity,
                                         pixel_count)
        && ensure_processing_u16_scratch(&processing->sharpen_mask_contour,
                                         &processing->sharpen_mask_capacity,
                                         pixel_count);
}


processingObject_t * initProcessingObject()
{
    processingObject_t * processing = calloc( 1, sizeof(processingObject_t) );

    processing->exr_mode = 0;
    processing->AgX = 1;

    processing->filter = initFilterObject();

    processing->lut = init_lut();
    processing->lut_on = 0;

    /* For precalculated matrix values */
    for (int i = 0; i < 9; ++i)
    {
        processing->pre_calc_matrix[i] = malloc( 65536 * sizeof(int32_t) );
        processing->pre_calc_matrix_gradient[i] = malloc( 65536 * sizeof(int32_t) );
    }

    /* A nothing matrix */
    // processing->cam_to_sRGB_matrix[0] = 1.0;
    // processing->cam_to_sRGB_matrix[4] = 1.0;
    // processing->cam_to_sRGB_matrix[8] = 1.0;
    // /* Different matrix BTW */
    // processing->xyz_to_rgb_matrix[0] = 1.0;
    // processing->xyz_to_rgb_matrix[4] = 1.0;
    // processing->xyz_to_rgb_matrix[8] = 1.0;

    /* Blur buffer images (may change size) */
    processing->shadows_highlights.blur_image = new_image_buffer();
    processing->shadows_highlights.blur_image_half_in = new_image_buffer();
    processing->shadows_highlights.blur_image_half_out = new_image_buffer();
    buffer_set_size(processing->shadows_highlights.blur_image, 2, 2); /* Fix craxh */
    buffer_set_size(processing->shadows_highlights.blur_image_half_in, 2, 2);
    buffer_set_size(processing->shadows_highlights.blur_image_half_out, 2, 2);

    double rgb_to_YCbCr[7] = {  0.299000,  0.587000,  0.114000,
                               -0.168736, -0.331264, /* 0.5 */
                               /* 0.5 */  -0.418688, -0.081312 };
    double YCbCr_to_rgb[4] = {             1.402000,
                               -0.344136, -0.714136,
                                1.772000  };

    for (int i = 0; i < 7; ++i)
    {
        processing->cs_zone.pre_calc_rgb_to_YCbCr[i] = malloc( 65536 * sizeof(int32_t) );
        for (int j = 0; j < 65536; ++j)
        {
            processing->cs_zone.pre_calc_rgb_to_YCbCr[i][j] = (double)j * rgb_to_YCbCr[i];
        }
    }
    for (int i = 0; i < 4; ++i)
    {
        processing->cs_zone.pre_calc_YCbCr_to_rgb[i] = malloc( 65536 * sizeof(int32_t) );
        for (int j = 0; j < 65536; ++j)
        {
            processing->cs_zone.pre_calc_YCbCr_to_rgb[i][j] = (double)(j-32768) * YCbCr_to_rgb[i];
        }
    }

    /* Set null */
    processing->transfer_function_string = NULL;
    processing->transfer_function_string_formatted = NULL;
    processing->x_variable.address = &processing->x_value;
    processing->x_variable.name = "x";
    processing->x_variable.context = 0;

    /* Gradient */
    processing->gradient_exposure_stops = 0.0;

    /* Default settings */
    processingSetWhiteBalance(processing, 6000.0, 0.0);
    processingSetBlackAndWhiteLevel(processing, 2048, 15000, 14); /* 14 bit! */
    processingSetExposureStops(processing, 0.0);
    processingSetTransferFunction(processing, "pow(x/(1+x), 1/3.15)");
    processingSetGamma(processing, STANDARD_GAMMA);
    processingSetGammaGradient(processing, STANDARD_GAMMA);
    processingSetVibrance(processing, 1.0);
    processingSetSaturation(processing, 1.0);
    processingSetContrast(processing, 0.73, 5.175, 0.5, 0.0, 0.0);
    processingSetPivot(processing, 0.75);
    processingSetImageProfile(processing, PROFILE_TONEMAPPED);
    processingSetSharpening(processing, 0.0);
    processingSetHighlights(processing, 0.0);
    processingSetShadows(processing, 0.0);
    processingSetSimpleContrast(processing, 0.0);
    processingSetTransformation(processing, TR_NONE);
    processingSetDenoiserStrength(processing, 0);
    processingSetDenoiserWindow(processing, 2);
    processingSetRbfDenoiserLuma(processing, 0);
    processingSetRbfDenoiserChroma(processing, 0);
    processingSetRbfDenoiserRange(processing, 40);
    processingUseCamMatrix(processing);
    processingAllowCreativeAdjustments(processing);
    processingSetGCurve(processing, 0, NULL, NULL, 0);
    processingSetGCurve(processing, 0, NULL, NULL, 1);
    processingSetGCurve(processing, 0, NULL, NULL, 2);
    processingSetGCurve(processing, 0, NULL, NULL, 3);
    processingSetHueVsCurves(processing, 0, NULL, NULL, 0);
    processingSetHueVsCurves(processing, 0, NULL, NULL, 1);
    processingSetHueVsCurves(processing, 0, NULL, NULL, 2);
    processingSetHueVsCurves(processing, 0, NULL, NULL, 3);
    processingSetVignetteStrength(processing, 0);
    processingSetDCFactor(processing, 4.5);
    processingSetDCRange(processing, 0.7);
    processingSetLCFactor(processing, 0);
    processingSetLCRange(processing, 0.5);

    processingSetGrainStrength(processing, 0);
    processingSetGrainLumaWeight(processing, 0);

    /* Colour default parameters */
    processingSetGamut(processing, GAMUT_Rec709);
    processingSetTonemappingFunction(processing, TONEMAP_Reinhard);
    processingSetGamma(processing, 3.15);
    processingSetGammaGradient(processing, 3.15);
    processingUseCamMatrix(processing);
    processingSetImageProfile(processing, PROFILE_TONEMAPPED);

    /* Just in case (should be done tho already) */
    processing_update_matrices(processing);
    //processing_update_matrices_gradient(processing);
    processing_update_shadow_highlight_curve(processing);

    processingSetToning(processing, 255, 192, 0, 0);
    processingSetCaDesaturate(processing, 0);
    processingSetCaRadius(processing, 1);

    return processing;
}


void processingSetGamut(processingObject_t * processing, int gamut)
{
    processing->colour_gamut = gamut;
    /* This will update everything necessary to enable tonemapping */
    processingSetWhiteBalance(processing, processingGetWhiteBalanceKelvin(processing), processingGetWhiteBalanceTint(processing));
    processingSetGamma(processing, processing->gamma_power);
    processingSetGammaGradient(processing, processing->gamma_power);
    processing_update_matrices(processing);
    processing_update_matrices_gradient(processing);
}

int processingGetGamut(processingObject_t * processing)
{
    return processing->colour_gamut;
}

void processingSetTonemappingFunction(processingObject_t * processing, int function)
{
    processing->tonemap_function = function;
    /* This will update everything necessary to enable tonemapping */
    processingSetGamma(processing, processing->gamma_power);
    processingSetGammaGradient(processing, processing->gamma_power);
    processing_update_matrices(processing);
    processing_update_matrices_gradient(processing);
}

int processingGetTonemappingFunction(processingObject_t * processing)
{
    return processing->tonemap_function;
}

void processingSetImageProfile(processingObject_t * processing, int imageProfile)
{
    /* Yes, we still have compatibility with old profile system */
    processingGetAllowedCreativeAdjustments(processing) = default_image_profiles[imageProfile].allow_creative_adjustments;
    processingSetGamut(processing, default_image_profiles[imageProfile].colour_gamut);
    processingSetTransferFunction(processing, default_image_profiles[imageProfile].transfer_function);

    /* This updates matrices, so new gamut will be put to use */
    processingSetWhiteBalance(processing, processingGetWhiteBalanceKelvin(processing), processingGetWhiteBalanceTint(processing));

    processing_update_matrices(processing);
    processing_update_matrices_gradient(processing);
}

/* Takes those matrices I learned about on the forum */
void processingSetCamMatrix(processingObject_t * processing, double * camMatrix, double * camMatrixA)
{
    memcpy(processing->cam_matrix, camMatrix, sizeof(double) * 9);
    memcpy(processing->cam_matrix_A, camMatrixA, sizeof(double) * 9);
    /* TO update matrices really argh so much confusion :( */
    processingSetWhiteBalance(processing, processingGetWhiteBalanceKelvin(processing), processingGetWhiteBalanceTint(processing));
    /* Calculates final main matrix */
    processing_update_matrices(processing);
    if( processing->gradient_enable != 0 ) processing_update_matrices_gradient(processing);
}

void processingSetHighlights(processingObject_t * processing, double value)
{
    if( processing->shadows_highlights.highlights == value )
        return;
    processing->shadows_highlights.highlights = value;
    processing_update_shadow_highlight_curve(processing);
}
void processingSetShadows(processingObject_t * processing, double value)
{
    if( processing->shadows_highlights.shadows == value )
        return;
    processing->shadows_highlights.shadows = value;
    processing_update_shadow_highlight_curve(processing);
}

void processing_update_shadow_highlight_curve(processingObject_t * processing)
{
    double shadows_expo = processing->shadows_highlights.shadows;
    double highlight_expo = pow(2.0, processing->shadows_highlights.highlights*(-1.5));
    #pragma omp parallel for
    for (int i = 1; i < 65536; ++i)
    {
        double expo_factor;
        double value = pow(((double)i)/65536.0, 0.75);

        double newvalue = add_contrast(value, 0.7, shadows_expo*0.5, 0.0, 0.0);

        if (newvalue > 0.2) {
            double fac = (newvalue - 0.2)*(1/0.8);
            newvalue = newvalue*(1.0-fac) + newvalue*highlight_expo*fac;
        }

        expo_factor = value/newvalue;

        processing->shadows_highlights.shadow_highlight_curve[i] = expo_factor;
    }
    processing->shadows_highlights.shadow_highlight_curve[0] = 1.0;
}

void processingSetSimpleContrast(processingObject_t * processing, double value)
{
    const double newValue = value * 0.65;
    if( processing->contrast == newValue )
        return;
    processing->contrast = newValue;
    processing_update_contrast_curve(processing);
}

void processingSetPivot(processingObject_t * processing, double value)
{
    if( processing->pivot == value )
        return;
    processing->pivot = value;
    processing_update_contrast_curve(processing);
}

void processing_update_contrast_curve(processingObject_t * processing)
{
    double shadows_expo = -processing->contrast;
    double highlight_expo = pow(2.0, processing->contrast*(-1.5));
    #pragma omp parallel for
    for (int i = 1; i < 65536; ++i)
    {
        double expo_factor;
        double value = pow(((double)i)/65536.0, processing->pivot);

        double newvalue = add_contrast(value, 0.7, shadows_expo*0.5, 0.0, 0.0);

        if (newvalue > 0.2) {
            double fac = (newvalue - 0.2)*(1/0.8);
            newvalue = newvalue*(1.0-fac) + newvalue*highlight_expo*fac;
        }

        expo_factor = value/newvalue;

        processing->contrast_curve[i] = expo_factor;
    }
    processing->contrast_curve[0] = 1.0;
}

void processingSetSimpleContrastGradient(processingObject_t * processing, double value)
{
    const double newValue = value * 0.65;
    if( processing->gradient_contrast == newValue )
        return;
    processing->gradient_contrast = newValue;
    processing_update_contrast_curve_gradient(processing);
}

void processing_update_contrast_curve_gradient(processingObject_t * processing)
{
    double shadows_expo = -processing->gradient_contrast;
    double highlight_expo = pow(2.0, processing->gradient_contrast*(-1.5));
    #pragma omp parallel for
    for (int i = 1; i < 65536; ++i)
    {
        double expo_factor;
        double value = pow(((double)i)/65536.0, 0.75);

        double newvalue = add_contrast(value, 0.7, shadows_expo*0.5, 0.0, 0.0);

        if (newvalue > 0.2) {
            double fac = (newvalue - 0.2)*(1/0.8);
            newvalue = newvalue*(1.0-fac) + newvalue*highlight_expo*fac;
        }

        expo_factor = value/newvalue;

        processing->gradient_contrast_curve[i] = expo_factor;
    }
    processing->gradient_contrast_curve[0] = 1.0;
}

void processingSetClarity(processingObject_t * processing, double value)
{
    if( value < 0 ) value /= 2.0;
    if( processing->clarity == value )
        return;
    processing->clarity = value;
    processing_update_clarity_curve(processing);
}

void processing_update_clarity_curve(processingObject_t * processing)
{
    double shadows_expo = -processing->clarity;
    double highlight_expo = pow(2.0, processing->clarity*(-1.5));
    #pragma omp parallel for
    for (int i = 1; i < 65536; ++i)
    {
        double expo_factor;
        double value = pow(((double)i)/65536.0, 0.75);

        double newvalue = add_contrast(value, 0.7, shadows_expo*0.5, 0.0, 0.0);

        if (newvalue > 0.2) {
            double fac = (newvalue - 0.2)*(1/0.8);
            newvalue = newvalue*(1.0-fac) + newvalue*highlight_expo*fac;
        }

        expo_factor = value/newvalue;

        processing->clarity_curve[i] = expo_factor;
    }
    processing->clarity_curve[0] = 1.0;
}

/* applyProcessingObject but with one argument for pthreading  */
void processing_object_thread(apply_processing_parameters_t * p)
{
    apply_processing_object( p->processing, 
                             p->imageX, p->imageY, 
                             p->inputImage, 
                             p->outputImage,
                             p->blurImage,
                             p->gradientMask,
                             p->vignetteMask,
                             p->core_timing );
}

/* Round-4 item 0c: the ONE shadows/highlights blur pre-pass, shared by the
 * 16-bit pipeline (applyProcessingObject) and the direct8 8-bit fast path
 * (applyProcessingObject8). The two paths used to carry separate copies of
 * the resolution policy; commit 25795f27 moved the 16-bit copy to half-res
 * for even-dimension frames and the 8-bit copy silently stayed full-res,
 * which broke direct8-vs-16-bit byte identity on every SH-active receipt
 * (the PhaseE9 parity failures). Identical inputs through THIS one function
 * produce identical blur bytes for both consumers - and the direct8 path
 * gets the half-res perf win it was missing. */
/* force_export_policy (round-4 item 2): the direct8 kernel's blurImage
 * contract is only byte-identity-proven against the export blur
 * configuration (PhaseE9 families: halfres/fullres by dimensions, no
 * preview-scale lanes). The standard-x2 quarterres lane writes content the
 * kernel misreads - VIEWED x2 filmstrips collapsed whole row-blocks to
 * lifted-black tan while x8 (which never takes the quarterres lane) stayed
 * clean. applyProcessingObject8 passes 1; the 16-bit path passes 0 and
 * keeps its scale-aware preview lanes. */
static void processing_compute_shadows_highlights_blur( processingObject_t * processing,
                                                        uint16_t * __restrict inputImage,
                                                        int imageX, int imageY, int threads,
                                                        int force_export_policy )
{
    g_processing_last_shadows_highlights_prep_run_count++;
    const int shadows_highlights_probe_enabled =
        processing_shadows_highlights_probe_mode() >= 0;
    const int halfres_even_dimensions =
        (imageX >= 2) && (imageY >= 2)
     && ((imageX & 1) == 0)
     && ((imageY & 1) == 0);
    const int preview_mode_enabled = !force_export_policy
        && processingPlaybackPreviewModeEnabled();
    const int aggressive_preview_enabled = preview_mode_enabled
        && processingPlaybackAggressivePreviewModeEnabled();
    const int halfres_aggressive_preview_odd_height =
        aggressive_preview_enabled
     && (imageX >= 2) && (imageY >= 3)
     && ((imageX & 1) == 0)
     && ((imageY & 1) != 0);
    const int preview_scale_factor = processingPlaybackPreviewScaleFactor();
    const int use_quarterres_rbf =
        (
            (preview_mode_enabled
             && !aggressive_preview_enabled
             && preview_scale_factor == 1
             && processing_standard_x1_shadows_highlights_quarterres_enabled())
         || (preview_mode_enabled
             && !aggressive_preview_enabled
             && preview_scale_factor == 2
             && processing_standard_x2_shadows_highlights_quarterres_enabled())
         || (preview_mode_enabled
             && !aggressive_preview_enabled
             && preview_scale_factor == 4
             && processing_standard_x4_shadows_highlights_quarterres_enabled())
         || (aggressive_preview_enabled
             && (
                    (preview_scale_factor == 2
                     && processing_aggressive_x2_shadows_highlights_quarterres_enabled())
                 || (preview_scale_factor >= 8
                     && processing_aggressive_x8_shadows_highlights_quarterres_enabled())
                ))
        )
     && imageX >= 4
     && imageY >= 4;
    g_processing_last_shadows_highlights_quarterres_path_taken = use_quarterres_rbf;
    const int use_halfres_rbf =
        halfres_even_dimensions || halfres_aggressive_preview_odd_height;

    const double shadows_highlights_filter_start = omp_get_wtime();
    if( use_quarterres_rbf )
    {
        const double quarterres_downsample_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        const int half_w = imageX >> 1;
        const int half_h = imageY >> 1;
        const int quarter_w = half_w >> 1;
        const int quarter_h = half_h >> 1;
        const float quarter_sigma_spatial = 0.0025f;
        buffer_set_size(processing->shadows_highlights.blur_image, imageX, imageY);
        buffer_set_size(processing->shadows_highlights.blur_image_half_in, half_w, half_h);
        buffer_set_size(processing->shadows_highlights.blur_image_half_out, quarter_w, quarter_h);

        rgb_u16_downsample_2x_box(inputImage,
                                  get_buffer(processing->shadows_highlights.blur_image_half_in),
                                  imageX,
                                  imageY,
                                  threads);
        rgb_u16_downsample_2x_box(get_buffer(processing->shadows_highlights.blur_image_half_in),
                                  get_buffer(processing->shadows_highlights.blur_image_half_out),
                                  half_w,
                                  half_h,
                                  threads);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_quarterres_downsample_ms +=
                (omp_get_wtime() - quarterres_downsample_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_quarterres_downsample_completed = 1;

        const double quarterres_rbf_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        buffer_set_size(processing->shadows_highlights.blur_image_half_in, quarter_w, quarter_h);
        if( processing_shadows_highlights_curve_index_mask_enabled() )
            recursive_bf_wrap_with_curve_index_lut(
                    get_buffer(processing->shadows_highlights.blur_image_half_out),
                    get_buffer(processing->shadows_highlights.blur_image_half_in),
                    quarter_sigma_spatial,
                    0.075f+(((float)100.0-40.0f)/666.6f),
                    quarter_w,
                    quarter_h,
                    3,
                    processing->pre_calc_levels,
                    processing->pre_calc_matrix[0],
                    processing->pre_calc_matrix[4],
                    processing->pre_calc_matrix[8]);
        else
            recursive_bf_wrap_with_output_lut(
                    get_buffer(processing->shadows_highlights.blur_image_half_out),
                    get_buffer(processing->shadows_highlights.blur_image_half_in),
                    quarter_sigma_spatial,
                    0.075f+(((float)100.0-40.0f)/666.6f),
                    quarter_w,
                    quarter_h,
                    3,
                    processing->pre_calc_levels);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_quarterres_rbf_ms +=
                (omp_get_wtime() - quarterres_rbf_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_quarterres_rbf_completed = 1;

        const double quarterres_upsample_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        buffer_set_size(processing->shadows_highlights.blur_image_half_out, half_w, half_h);
        rgb_u16_upsample_2x_bilinear_to_size(
            get_buffer(processing->shadows_highlights.blur_image_half_in),
            get_buffer(processing->shadows_highlights.blur_image_half_out),
            quarter_w,
            quarter_h,
            half_w,
            half_h,
            threads);
        rgb_u16_upsample_2x_bilinear_to_size(
            get_buffer(processing->shadows_highlights.blur_image_half_out),
            get_buffer(processing->shadows_highlights.blur_image),
            half_w,
            half_h,
            imageX,
            imageY,
            threads);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_quarterres_upsample_ms +=
                (omp_get_wtime() - quarterres_upsample_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_quarterres_upsample_completed = 1;
    }
    else if( use_halfres_rbf )
    {
        const double halfres_downsample_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        const int half_w = imageX >> 1;
        const int half_h = imageY >> 1;
        const int halfres_output_h = half_h << 1;
        const float half_sigma_spatial = 0.0025f * 0.5f;
        buffer_set_size(processing->shadows_highlights.blur_image, imageX, imageY);
        buffer_set_size(processing->shadows_highlights.blur_image_half_in, half_w, half_h);
        buffer_set_size(processing->shadows_highlights.blur_image_half_out, half_w, half_h);

        rgb_u16_downsample_2x_box(inputImage,
                                  get_buffer(processing->shadows_highlights.blur_image_half_in),
                                  imageX,
                                  imageY,
                                  threads);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_halfres_downsample_ms +=
                (omp_get_wtime() - halfres_downsample_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_halfres_downsample_completed = 1;
        const double halfres_rbf_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        if( processing_shadows_highlights_curve_index_mask_enabled() )
            recursive_bf_wrap_with_curve_index_lut(
                    get_buffer(processing->shadows_highlights.blur_image_half_in),
                    get_buffer(processing->shadows_highlights.blur_image_half_out),
                    half_sigma_spatial,
                    0.075f+(((float)100.0-40.0f)/666.6f),
                    half_w,
                    half_h,
                    3,
                    processing->pre_calc_levels,
                    processing->pre_calc_matrix[0],
                    processing->pre_calc_matrix[4],
                    processing->pre_calc_matrix[8]);
        else
            recursive_bf_wrap_with_output_lut(
                    get_buffer(processing->shadows_highlights.blur_image_half_in),
                    get_buffer(processing->shadows_highlights.blur_image_half_out),
                    half_sigma_spatial,
                    0.075f+(((float)100.0-40.0f)/666.6f),
                    half_w,
                    half_h,
                    3,
                    processing->pre_calc_levels);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_halfres_rbf_ms +=
                (omp_get_wtime() - halfres_rbf_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_halfres_rbf_completed = 1;
        const double halfres_upsample_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        rgb_u16_upsample_2x_bilinear(get_buffer(processing->shadows_highlights.blur_image_half_out),
                                     get_buffer(processing->shadows_highlights.blur_image),
                                     half_w,
                                     half_h,
                                     threads);
        if( halfres_output_h < imageY )
        {
            uint16_t * blur = get_buffer(processing->shadows_highlights.blur_image);
            const size_t row_words = (size_t)imageX * 3u;
            const uint16_t * src_row =
                blur + (size_t)(halfres_output_h - 1) * row_words;
            for( int y = halfres_output_h; y < imageY; ++y )
            {
                memcpy(blur + (size_t)y * row_words,
                       src_row,
                       row_words * sizeof(uint16_t));
            }
        }
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_halfres_upsample_ms +=
                (omp_get_wtime() - halfres_upsample_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_halfres_upsample_completed = 1;
    }
    else if( processing_shadows_highlights_curve_index_mask_enabled() )
    {
        const double fullres_rbf_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        recursive_bf_wrap_with_curve_index_lut(
                inputImage,
                get_buffer(processing->shadows_highlights.blur_image),
                0.0005f, 0.075f+(((float)100.0-40.0f)/666.6f),
                imageX, imageY, 3,
                processing->pre_calc_levels,
                processing->pre_calc_matrix[0],
                processing->pre_calc_matrix[4],
                processing->pre_calc_matrix[8]);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_fullres_ms +=
                (omp_get_wtime() - fullres_rbf_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_fullres_completed = 1;
    }
    else
    {
        const double fullres_rbf_start =
            shadows_highlights_probe_enabled ? omp_get_wtime() : 0.0;
        recursive_bf_wrap_with_output_lut(
                inputImage,
                get_buffer(processing->shadows_highlights.blur_image),
                0.0005f, 0.075f+(((float)100.0-40.0f)/666.6f),
                imageX, imageY, 3,
                processing->pre_calc_levels);
        if( shadows_highlights_probe_enabled )
        {
            g_processing_last_shadows_highlights_filter_fullres_ms +=
                (omp_get_wtime() - fullres_rbf_start) * 1000.0;
        }
        g_processing_last_shadows_highlights_fullres_completed = 1;
    }
    processing_capture_last_shadows_highlights_rbf_timing();
    g_processing_last_shadows_highlights_filter_ms +=
        (omp_get_wtime() - shadows_highlights_filter_start) * 1000.0;
    g_processing_last_shadows_highlights_filter_run_count++;
}

int processingRefreshShadowsHighlightsBlurFromRgb16(processingObject_t * processing,
                                                    uint16_t * inputImage,
                                                    int width,
                                                    int height,
                                                    int threads,
                                                    int forceExportPolicy)
{
    if( !processing || !inputImage || width <= 0 || height <= 0 )
    {
        return 0;
    }

    const int shadows_highlights_active =
        ( processing->shadows_highlights.shadows <= -0.01 || processing->shadows_highlights.shadows >= 0.01 )
     || ( processing->shadows_highlights.highlights <= -0.01 || processing->shadows_highlights.highlights >= 0.01 )
     || ( processing->clarity <= -0.01 || processing->clarity >= 0.01 );
    if( !shadows_highlights_active )
    {
        return 0;
    }

    g_processing_last_shadows_highlights_resize_ms = 0.0;
    g_processing_last_shadows_highlights_copy_ms = 0.0;
    g_processing_last_shadows_highlights_filter_ms = 0.0;
    g_processing_last_shadows_highlights_filter_fullres_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_downsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_rbf_ms = 0.0;
    g_processing_last_shadows_highlights_filter_halfres_upsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_downsample_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_rbf_ms = 0.0;
    g_processing_last_shadows_highlights_filter_quarterres_upsample_ms = 0.0;
    g_processing_last_shadows_highlights_quarterres_downsample_completed = 0;
    g_processing_last_shadows_highlights_quarterres_rbf_completed = 0;
    g_processing_last_shadows_highlights_quarterres_upsample_completed = 0;
    g_processing_last_shadows_highlights_halfres_downsample_completed = 0;
    g_processing_last_shadows_highlights_halfres_rbf_completed = 0;
    g_processing_last_shadows_highlights_halfres_upsample_completed = 0;
    g_processing_last_shadows_highlights_fullres_completed = 0;
    g_processing_last_shadows_highlights_prep_run_count = 0;
    g_processing_last_shadows_highlights_filter_run_count = 0;
    g_processing_last_shadows_highlights_rbf_total_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_boundary_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_range_table_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_left_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_right_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_horizontal_average_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_down_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_first_line_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_diff_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_factor_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_src_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_prev_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_assign_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_vertical_up_ms = 0.0;
    g_processing_last_shadows_highlights_rbf_output_ms = 0.0;

    const double start = omp_get_wtime();
    const double resize_start = omp_get_wtime();
    buffer_set_size(processing->shadows_highlights.blur_image, width, height);
    g_processing_last_shadows_highlights_resize_ms =
        (omp_get_wtime() - resize_start) * 1000.0;
    processing_compute_shadows_highlights_blur(
        processing, inputImage, width, height, threads, forceExportPolicy);
    g_processing_last_shadows_highlights_prep_ms =
        (omp_get_wtime() - start) * 1000.0;
    return 1;
}

/* Apply it with multiple threads */
void applyProcessingObject( processingObject_t * processing,
                            int imageX, int imageY,
                            uint16_t * __restrict inputImage,
                            uint16_t * __restrict outputImage,
                            int threads, int imageChanged, uint64_t frameIndex )
{
    processingResetLastTimingTelemetry();

    const double setup_start = omp_get_wtime();
    /* Do transformation */
    get_frame_transformed(processing, inputImage, imageX, imageY);

    int img_s = imageX * imageY * 3;
    //Noise shall not move for the same picture
    uint32_t randomseed1 = ((uint32_t *)inputImage)[0] ^ ((uint32_t *)(inputImage+img_s))[-1] ^ frameIndex;
    uint32_t randomseed2 = ((uint32_t *)inputImage)[1] ^ ((uint32_t *)(inputImage+img_s/2))[0] ^ frameIndex;
    uint32_t randomseed3 = ((uint32_t *)inputImage)[2] ^ ((uint32_t *)(inputImage+img_s/3))[0] ^ frameIndex;
    uint32_t randomseed4 = ((uint32_t *)inputImage)[3] ^ ((uint32_t *)(inputImage+img_s/4))[0] ^ frameIndex;
    g_processing_last_setup_ms = (omp_get_wtime() - setup_start) * 1000.0;

    /* Resize image buffer to make sure its right size */
    if (imageChanged)
    {
        const double shadows_highlights_resize_start = omp_get_wtime();
        buffer_set_size(processing->shadows_highlights.blur_image, imageX, imageY);
        g_processing_last_shadows_highlights_resize_ms =
            (omp_get_wtime() - shadows_highlights_resize_start) * 1000.0;
    }

    /* If shadows/highlights off don't do anything. Maybe this blurring bit could b multithreaded I need to think */
    const double shadows_highlights_start = omp_get_wtime();
    const int shadows_highlights_active =
        ( processing->shadows_highlights.shadows <= -0.01 || processing->shadows_highlights.shadows >= 0.01 )
     || ( processing->shadows_highlights.highlights <= -0.01 || processing->shadows_highlights.highlights >= 0.01 )
     || ( processing->clarity <= -0.01 || processing->clarity >= 0.01 );

    /* When shadows/highlights and clarity are inactive, the blur buffer is not
     * read by the 16-bit path. Keep the resized scratch buffer around, but skip
     * the frame-sized copy. */

    /* Round-4 item 0c: the blur pre-pass lives in
     * processing_compute_shadows_highlights_blur so the direct8 8-bit path
     * computes byte-identical blur bytes (the PhaseE9 parity fix). */
    if( shadows_highlights_active && imageChanged )
    {
        processing_compute_shadows_highlights_blur( processing, inputImage, imageX, imageY, threads, 0 );
    }
    g_processing_last_shadows_highlights_prep_ms = (omp_get_wtime() - shadows_highlights_start) * 1000.0;

    /* Analyse dual iso frame to find highest green for highlight reconstruction */
    if( processing->highlight_reconstruction )
    {
        const double highest_green_start = omp_get_wtime();
        analyse_frame_highest_green( processing, imageX, imageY, inputImage );
        g_processing_last_highest_green_ms = (omp_get_wtime() - highest_green_start) * 1000.0;
    }

    /* If threads is 1, no threads are needed */
    const double core_start = omp_get_wtime();
    if (threads == 1)
    {
        processing_core_timing_t core_timing;
        processing_core_timing_reset(&core_timing);
        apply_processing_object(processing,
                                imageX,
                                imageY,
                                inputImage,
                                outputImage,
                                get_buffer(processing->shadows_highlights.blur_image),
                                processing->gradient_mask,
                                processing->vignette_mask,
                                &core_timing);
        g_processing_last_core_levels_ms = core_timing.levels_ms;
        g_processing_last_core_color_ms = core_timing.color_ms;
        g_processing_last_core_color_main_ms = core_timing.color_main_ms;
        g_processing_last_core_color_gradient_ms = core_timing.color_gradient_ms;
        g_processing_last_core_color_main_prelude_ms = core_timing.color_main_prelude_ms;
        g_processing_last_core_color_main_prelude_vignette_ms = core_timing.color_main_prelude_vignette_ms;
        g_processing_last_core_color_main_prelude_creative_ms = core_timing.color_main_prelude_creative_ms;
        g_processing_last_core_color_main_prelude_creative_shadows_ms = core_timing.color_main_prelude_creative_shadows_ms;
        g_processing_last_core_color_main_prelude_creative_contrast_ms = core_timing.color_main_prelude_creative_contrast_ms;
        g_processing_last_core_color_main_prelude_wb_ms = core_timing.color_main_prelude_wb_ms;
        g_processing_last_core_color_main_prelude_wb_matrix_ms = core_timing.color_main_prelude_wb_matrix_ms;
        g_processing_last_core_color_main_prelude_wb_matrix_r_ms = core_timing.color_main_prelude_wb_matrix_r_ms;
        g_processing_last_core_color_main_prelude_wb_matrix_g_ms = core_timing.color_main_prelude_wb_matrix_g_ms;
        g_processing_last_core_color_main_prelude_wb_matrix_b_ms = core_timing.color_main_prelude_wb_matrix_b_ms;
        g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms = core_timing.color_main_prelude_wb_gradient_matrix_ms;
        g_processing_last_core_color_main_prelude_wb_exposure_ms = core_timing.color_main_prelude_wb_exposure_ms;
        g_processing_last_core_color_main_prelude_wb_gamut_ms = core_timing.color_main_prelude_wb_gamut_ms;
        g_processing_last_core_color_main_prelude_wb_recon_ms = core_timing.color_main_prelude_wb_recon_ms;
        g_processing_last_core_color_cam_ms = core_timing.color_cam_ms;
        g_processing_last_core_color_cam_main_ms = core_timing.color_cam_main_ms;
        g_processing_last_core_color_cam_gradient_ms = core_timing.color_cam_gradient_ms;
        g_processing_last_core_color_cam_wb_ms = core_timing.color_cam_wb_ms;
        g_processing_last_core_color_cam_wb_matrix_ms = core_timing.color_cam_wb_matrix_ms;
        g_processing_last_core_color_cam_wb_gamut_ms = core_timing.color_cam_wb_gamut_ms;
        g_processing_last_core_color_cam_wb_desat_ms = core_timing.color_cam_wb_desat_ms;
        g_processing_last_core_color_cam_agx_ms = core_timing.color_cam_agx_ms;
        g_processing_last_core_color_cam_agx_clip_ms = core_timing.color_cam_agx_clip_ms;
        g_processing_last_core_color_cam_agx_clip_neg_r_count = core_timing.color_cam_agx_clip_neg_r_count;
        g_processing_last_core_color_cam_agx_clip_neg_g_count = core_timing.color_cam_agx_clip_neg_g_count;
        g_processing_last_core_color_cam_agx_clip_neg_b_count = core_timing.color_cam_agx_clip_neg_b_count;
        g_processing_last_core_color_cam_agx_matrix_ms = core_timing.color_cam_agx_matrix_ms;
        g_processing_last_core_color_cam_agx_matrix_r_ms = core_timing.color_cam_agx_matrix_r_ms;
        g_processing_last_core_color_cam_agx_matrix_g_ms = core_timing.color_cam_agx_matrix_g_ms;
        g_processing_last_core_color_cam_agx_matrix_b_ms = core_timing.color_cam_agx_matrix_b_ms;
        g_processing_last_core_color_cam_agx_matrix_r_hi_count = core_timing.color_cam_agx_matrix_r_hi_count;
        g_processing_last_core_color_cam_agx_matrix_g_hi_count = core_timing.color_cam_agx_matrix_g_hi_count;
        g_processing_last_core_color_cam_agx_matrix_b_hi_count = core_timing.color_cam_agx_matrix_b_hi_count;
        g_processing_last_core_color_gamma_ms = core_timing.color_gamma_ms;
        g_processing_last_core_color_gamma_main_ms = core_timing.color_gamma_main_ms;
        g_processing_last_core_color_gamma_gradient_ms = core_timing.color_gamma_gradient_ms;
        g_processing_last_core_creative_ms = core_timing.creative_ms;
        g_processing_last_core_creative_hue_vs_ms = core_timing.creative_hue_vs_ms;
        g_processing_last_core_creative_vibrance_ms = core_timing.creative_vibrance_ms;
        g_processing_last_core_creative_saturation_ms = core_timing.creative_saturation_ms;
        g_processing_last_core_creative_toning_ms = core_timing.creative_toning_ms;
        g_processing_last_core_creative_curve_ms = core_timing.creative_curve_ms;
        g_processing_last_core_creative_gradation_ms = core_timing.creative_gradation_ms;
        g_processing_last_core_creative_agx_inverse_ms = core_timing.creative_agx_inverse_ms;
        g_processing_last_core_output_ms = core_timing.output_ms;
    }
    else
    {
        apply_processing_parameters_t * params = alloca(sizeof(apply_processing_parameters_t) * threads);
        processing_core_timing_t * core_timings = alloca(sizeof(processing_core_timing_t) * threads);

        /* All chunks this height except possibly slightly longer last one */
        int chunk_size = imageY/threads;
        /* Size of a chunk */
        uint32_t offset_chunk = imageX * chunk_size * 3;

        /* Split in to chunks for each thread */
        for (int t = 0; t < threads; ++t)
        {
            params[t].processing = processing;
            params[t].imageX = imageX;
            params[t].imageY = chunk_size;
            params[t].inputImage = inputImage + offset_chunk*t;
            params[t].outputImage = outputImage + offset_chunk*t;
            params[t].blurImage = get_buffer(processing->shadows_highlights.blur_image) + offset_chunk*t;
            params[t].gradientMask = processing->gradient_mask
                ? processing->gradient_mask + (imageX * chunk_size * t)
                : NULL;
            params[t].vignetteMask = processing->vignette_mask
                ? processing->vignette_mask + (imageX * chunk_size * t)
                : NULL;
            params[t].core_timing = core_timings + t;
            processing_core_timing_reset(params[t].core_timing);
        }

        /* To make sure bottom is processed */
        params[threads-1].imageY = imageY - chunk_size * (threads-1);

        pthread_t * threadid = alloca(threads * sizeof(pthread_t));

        /* Do threads */
        for (int t = 0; t < threads; ++t)
        {
            pthread_create(&threadid[t], NULL, (void *)&processing_object_thread, (void *)(params + t));
        }
        /* let all threads finish */
        for (int t = 0; t < threads; ++t)
        {
            pthread_join(threadid[t], NULL);
        }

        for (int t = 0; t < threads; ++t)
        {
            g_processing_last_core_levels_ms =
                MAX(g_processing_last_core_levels_ms, core_timings[t].levels_ms);
            g_processing_last_core_color_ms =
                MAX(g_processing_last_core_color_ms, core_timings[t].color_ms);
            g_processing_last_core_color_main_ms =
                MAX(g_processing_last_core_color_main_ms, core_timings[t].color_main_ms);
            g_processing_last_core_color_gradient_ms =
                MAX(g_processing_last_core_color_gradient_ms, core_timings[t].color_gradient_ms);
            g_processing_last_core_color_main_prelude_ms =
                MAX(g_processing_last_core_color_main_prelude_ms, core_timings[t].color_main_prelude_ms);
            g_processing_last_core_color_main_prelude_vignette_ms =
                MAX(g_processing_last_core_color_main_prelude_vignette_ms, core_timings[t].color_main_prelude_vignette_ms);
            g_processing_last_core_color_main_prelude_creative_ms =
                MAX(g_processing_last_core_color_main_prelude_creative_ms, core_timings[t].color_main_prelude_creative_ms);
            g_processing_last_core_color_main_prelude_creative_shadows_ms =
                MAX(g_processing_last_core_color_main_prelude_creative_shadows_ms, core_timings[t].color_main_prelude_creative_shadows_ms);
            g_processing_last_core_color_main_prelude_creative_contrast_ms =
                MAX(g_processing_last_core_color_main_prelude_creative_contrast_ms, core_timings[t].color_main_prelude_creative_contrast_ms);
            g_processing_last_core_color_main_prelude_wb_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_ms, core_timings[t].color_main_prelude_wb_ms);
            g_processing_last_core_color_main_prelude_wb_matrix_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_matrix_ms, core_timings[t].color_main_prelude_wb_matrix_ms);
            g_processing_last_core_color_main_prelude_wb_matrix_r_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_matrix_r_ms, core_timings[t].color_main_prelude_wb_matrix_r_ms);
            g_processing_last_core_color_main_prelude_wb_matrix_g_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_matrix_g_ms, core_timings[t].color_main_prelude_wb_matrix_g_ms);
            g_processing_last_core_color_main_prelude_wb_matrix_b_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_matrix_b_ms, core_timings[t].color_main_prelude_wb_matrix_b_ms);
            g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms, core_timings[t].color_main_prelude_wb_gradient_matrix_ms);
            g_processing_last_core_color_main_prelude_wb_exposure_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_exposure_ms, core_timings[t].color_main_prelude_wb_exposure_ms);
            g_processing_last_core_color_main_prelude_wb_gamut_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_gamut_ms, core_timings[t].color_main_prelude_wb_gamut_ms);
            g_processing_last_core_color_main_prelude_wb_recon_ms =
                MAX(g_processing_last_core_color_main_prelude_wb_recon_ms, core_timings[t].color_main_prelude_wb_recon_ms);
            g_processing_last_core_color_cam_ms =
                MAX(g_processing_last_core_color_cam_ms, core_timings[t].color_cam_ms);
            g_processing_last_core_color_cam_main_ms =
                MAX(g_processing_last_core_color_cam_main_ms, core_timings[t].color_cam_main_ms);
            g_processing_last_core_color_cam_gradient_ms =
                MAX(g_processing_last_core_color_cam_gradient_ms, core_timings[t].color_cam_gradient_ms);
            g_processing_last_core_color_cam_wb_ms =
                MAX(g_processing_last_core_color_cam_wb_ms, core_timings[t].color_cam_wb_ms);
            g_processing_last_core_color_cam_wb_matrix_ms =
                MAX(g_processing_last_core_color_cam_wb_matrix_ms, core_timings[t].color_cam_wb_matrix_ms);
            g_processing_last_core_color_cam_wb_gamut_ms =
                MAX(g_processing_last_core_color_cam_wb_gamut_ms, core_timings[t].color_cam_wb_gamut_ms);
            g_processing_last_core_color_cam_wb_desat_ms =
                MAX(g_processing_last_core_color_cam_wb_desat_ms, core_timings[t].color_cam_wb_desat_ms);
            g_processing_last_core_color_cam_agx_ms =
                MAX(g_processing_last_core_color_cam_agx_ms, core_timings[t].color_cam_agx_ms);
            g_processing_last_core_color_cam_agx_clip_ms =
                MAX(g_processing_last_core_color_cam_agx_clip_ms, core_timings[t].color_cam_agx_clip_ms);
            g_processing_last_core_color_cam_agx_clip_neg_r_count += core_timings[t].color_cam_agx_clip_neg_r_count;
            g_processing_last_core_color_cam_agx_clip_neg_g_count += core_timings[t].color_cam_agx_clip_neg_g_count;
            g_processing_last_core_color_cam_agx_clip_neg_b_count += core_timings[t].color_cam_agx_clip_neg_b_count;
            g_processing_last_core_color_cam_agx_matrix_ms =
                MAX(g_processing_last_core_color_cam_agx_matrix_ms, core_timings[t].color_cam_agx_matrix_ms);
            g_processing_last_core_color_cam_agx_matrix_r_ms =
                MAX(g_processing_last_core_color_cam_agx_matrix_r_ms, core_timings[t].color_cam_agx_matrix_r_ms);
            g_processing_last_core_color_cam_agx_matrix_g_ms =
                MAX(g_processing_last_core_color_cam_agx_matrix_g_ms, core_timings[t].color_cam_agx_matrix_g_ms);
            g_processing_last_core_color_cam_agx_matrix_b_ms =
                MAX(g_processing_last_core_color_cam_agx_matrix_b_ms, core_timings[t].color_cam_agx_matrix_b_ms);
            g_processing_last_core_color_cam_agx_matrix_r_hi_count += core_timings[t].color_cam_agx_matrix_r_hi_count;
            g_processing_last_core_color_cam_agx_matrix_g_hi_count += core_timings[t].color_cam_agx_matrix_g_hi_count;
            g_processing_last_core_color_cam_agx_matrix_b_hi_count += core_timings[t].color_cam_agx_matrix_b_hi_count;
            g_processing_last_core_color_gamma_ms =
                MAX(g_processing_last_core_color_gamma_ms, core_timings[t].color_gamma_ms);
            g_processing_last_core_color_gamma_main_ms =
                MAX(g_processing_last_core_color_gamma_main_ms, core_timings[t].color_gamma_main_ms);
            g_processing_last_core_color_gamma_gradient_ms =
                MAX(g_processing_last_core_color_gamma_gradient_ms, core_timings[t].color_gamma_gradient_ms);
            g_processing_last_core_creative_ms =
                MAX(g_processing_last_core_creative_ms, core_timings[t].creative_ms);
            g_processing_last_core_creative_hue_vs_ms =
                MAX(g_processing_last_core_creative_hue_vs_ms, core_timings[t].creative_hue_vs_ms);
            g_processing_last_core_creative_vibrance_ms =
                MAX(g_processing_last_core_creative_vibrance_ms, core_timings[t].creative_vibrance_ms);
            g_processing_last_core_creative_saturation_ms =
                MAX(g_processing_last_core_creative_saturation_ms, core_timings[t].creative_saturation_ms);
            g_processing_last_core_creative_toning_ms =
                MAX(g_processing_last_core_creative_toning_ms, core_timings[t].creative_toning_ms);
            g_processing_last_core_creative_curve_ms =
                MAX(g_processing_last_core_creative_curve_ms, core_timings[t].creative_curve_ms);
            g_processing_last_core_creative_gradation_ms =
                MAX(g_processing_last_core_creative_gradation_ms, core_timings[t].creative_gradation_ms);
            g_processing_last_core_creative_agx_inverse_ms =
                MAX(g_processing_last_core_creative_agx_inverse_ms, core_timings[t].creative_agx_inverse_ms);
            g_processing_last_core_output_ms =
                MAX(g_processing_last_core_output_ms, core_timings[t].output_ms);
        }
    }
    g_processing_last_core_ms = (omp_get_wtime() - core_start) * 1000.0;

    /* Denoiser must render on complete image, because of 2D median border problem */
    if( processing->denoiserStrength > 0 )
    {
        const double denoise_start = omp_get_wtime();
        denoise_2D_median_with_context(
            outputImage,
            imageX,
            imageY,
            processing->denoiserWindow,
            processing->denoiserStrength,
            &processing->denoiser_context);
        g_processing_last_denoise_ms = (omp_get_wtime() - denoise_start) * 1000.0;
    }

    /* Recursive bilateral filtering (developed by Qingxiong Yang) must render on complete image, because of border problems */
    if( processing->rbfDenoiserLuma > 0 || processing->rbfDenoiserChroma > 0 )
    {
        const double rbf_start = omp_get_wtime();
        int img_s = imageX * imageY * 3;
        memcpy( inputImage, outputImage, img_s * sizeof(uint16_t) );
        recursive_bf_wrap(
                inputImage,
                outputImage,
                0.0025f, 0.075f+(((float)processing->rbfDenoiserRange-40.0f)/666.6f),
                imageX, imageY, 3);

        float outL = processing->rbfDenoiserLuma/100.0;
        float inL = 1.0 - outL;
        float outC = processing->rbfDenoiserChroma/100.0;
        float inC = 1.0 - outC;

        convert_rgb_to_YCbCr_omp(inputImage, img_s, processing->cs_zone.pre_calc_rgb_to_YCbCr);
        convert_rgb_to_YCbCr_omp(outputImage, img_s, processing->cs_zone.pre_calc_rgb_to_YCbCr);
        /* Linear blend strengths */
#pragma omp parallel for
        for( int i = 0; i < img_s; i+=3 )
        {
            outputImage[i+0] = outputImage[i+0]*outL + inputImage[i+0]*inL;
            outputImage[i+1] = outputImage[i+1]*outC + inputImage[i+1]*inC;
            outputImage[i+2] = outputImage[i+2]*outC + inputImage[i+2]*inC;
        }
        convert_YCbCr_to_rgb_omp(outputImage, img_s, processing->cs_zone.pre_calc_YCbCr_to_rgb);
        g_processing_last_rbf_ms = (omp_get_wtime() - rbf_start) * 1000.0;
    }
    /* RGB CA&ColorMoiree Removal */
    if( processing->ca_desaturate > 0 )
    {
        const double ca_start = omp_get_wtime();
        int img_s = imageX * imageY * 3;
        memcpy( inputImage, outputImage, img_s * sizeof(uint16_t) );
        CACorrection(imageX, imageY, inputImage, outputImage,
                     (uint16_t)(100-processing->ca_desaturate)<<9,
                     processing->ca_radius);
        g_processing_last_ca_ms = (omp_get_wtime() - ca_start) * 1000.0;
    }


    uint8_t doChromaSeperation = processingUsesChromaSeparation(processing);
    const int doSharpening = processingGetSharpening(processing) > 0.005;
    const int doGrain = processing->grainStrength > 0;
    if( !doChromaSeperation && !doSharpening && !doGrain )
    {
        return;
    }

    uint32_t sharp_skip = 1; /* Skip how many pixels when applying sharpening */
    uint32_t sharp_start = 0; /* How many pixels offset to start at */
    memcpy( inputImage, outputImage, img_s * sizeof(uint16_t) );

    /* enter YCbCr world - https://en.wikipedia.org/wiki/YCbCr (I used the 'JPEG Transform') */
    if (doChromaSeperation)
    {
        const double chroma_start = omp_get_wtime();
        convert_rgb_to_YCbCr_omp(inputImage, img_s, processing->cs_zone.pre_calc_rgb_to_YCbCr);

        sharp_start = 0; /* Start at 0 - Luma/Y channel */
        sharp_skip = 3; /* Only sharpen every third (Y/luma) pixel */

        /* Basic box blur */
        if (processingGetChromaBlurRadius(processing) > 0)
        {
            memcpy(outputImage, inputImage, img_s * sizeof(uint16_t));
            blur_image( inputImage, outputImage,
                        imageX, imageY, processingGetChromaBlurRadius(processing),
                        0,1,1,
                        0,0 );
        }
        g_processing_last_chroma_ms += (omp_get_wtime() - chroma_start) * 1000.0;
    }

    if( doSharpening )
    {
        const double sharpen_start = omp_get_wtime();
        /* Use sobel filter to create a edge mask */
        uint16_t *gray,
             *sobel_h_res,
             *sobel_v_res,
             *contour_img;
        int release_sobel_scratch = 0;
        if( processing->sh_masking > 0 )
        {
            const size_t pixel_count = (size_t)imageX * (size_t)imageY;
            if( ensure_sharpen_mask_scratch(processing, pixel_count) )
            {
                gray = processing->sharpen_mask_gray;
                sobel_h_res = processing->sharpen_mask_sobel_h;
                sobel_v_res = processing->sharpen_mask_sobel_v;
                contour_img = processing->sharpen_mask_contour;
                sobelFilterInto(inputImage, gray, sobel_h_res, sobel_v_res, contour_img, imageX, imageY);
            }
            else
            {
                sobelFilter(inputImage, &gray, &sobel_h_res, &sobel_v_res, &contour_img, imageX, imageY);
                release_sobel_scratch = 1;
            }
        }

        /* Avoid gaps in pixels if skipping pixels during sharpen */
        if (sharp_skip != 1) memcpy(outputImage, inputImage, img_s * sizeof(uint16_t));

        uint32_t y_max = imageY - 1;
        uint32_t x_max = (imageX - 1) * 3; /* X in multiples of 3 for RGB */

        /* Center and outter lut */
        uint32_t * ka = processing->pre_calc_sharp_a;
        uint16_t * kx = processing->pre_calc_sharp_x;
        uint16_t * ky = processing->pre_calc_sharp_y;

        /* Row length elements */
        uint32_t rl = imageX * 3;

#pragma omp parallel for
        for (uint32_t y = 0; y < y_max+1; ++y)
        {
            uint16_t * out_row = outputImage + (y * rl); /* current row ouptut */
            uint16_t * row = inputImage + (y * rl); /* current row */
            uint16_t * p_row;
            if( y == 0 ) p_row = row;        /* minimize border artifact */
            else p_row = inputImage + ((y-1) * rl); /* previous */

            uint16_t * n_row;
            if( y == y_max ) p_row = row;    /* minimize border artifact */
            else n_row = inputImage + ((y+1) * rl); /* next */

            uint16_t * cont_row;
            if( processing->sh_masking > 0 )
            {
                cont_row = contour_img + (y * imageX);
            }

            for (uint32_t x = 3+sharp_start; x < x_max; x+=sharp_skip)
            {
                int32_t sharp = ka[row[x]]
                              - ky[p_row[x]]
                              - ky[n_row[x]]
                              - kx[row[x-3]]
                              - kx[row[x+3]];

                /* use the edge mask for sharpening only edges */
                if( processing->sh_masking > 0 )
                {
                    uint32_t x1 = x / 3;
                    /* more contrast & brightness for mask */
                    uint32_t maskIntensity = 15000;
                    uint32_t cont = cont_row[x1] + (100-(uint32_t)processing->sh_masking) * 150;
                    if( cont > maskIntensity ) cont = maskIntensity;
                    /* calc output in dependency to mask slider */
                    out_row[x] = LIMIT16( ( cont / (float)maskIntensity) * LIMIT16(sharp)
                                      + ( ( maskIntensity - cont ) / (float)maskIntensity ) * row[x] );
                    /* Show mask */
                    //out_row[x] = LIMIT16(cont/(float)maskIntensity*65535.0);
                }
                /* sharpen all */
                else
                {
                    out_row[x] = LIMIT16(sharp);
                }
            }

            /* Edge pixels (basically don't do any changes to them) */
            out_row[0] = row[0];
            out_row[1] = row[1];
            out_row[2] = row[2];
            out_row += rl;
            row += rl;
            out_row[-3] = row[-3];
            out_row[-2] = row[-2];
            out_row[-1] = row[-1];
        }

        /* Copy top and bottom row */
        //memcpy(outputImage, inputImage, rl * sizeof(uint16_t));
        //memcpy(outputImage + (rl*(imageY-1)), inputImage + (rl*(imageY-1)), rl * sizeof(uint16_t));

        if( processing->sh_masking > 0 && release_sobel_scratch )
        {
            if( gray ) free( gray );
            if( sobel_h_res ) free( sobel_h_res );
            if( sobel_v_res ) free( sobel_v_res );
            if( contour_img ) free( contour_img );
        }
        g_processing_last_sharpen_ms = (omp_get_wtime() - sharpen_start) * 1000.0;
    }
    else
    {
        memcpy(outputImage, inputImage, img_s * sizeof(uint16_t));
    }

    /* Leave Y-Cb-Cr world */
    if (doChromaSeperation)
    {
        const double chroma_start = omp_get_wtime();
        convert_YCbCr_to_rgb_omp(outputImage, img_s, processing->cs_zone.pre_calc_YCbCr_to_rgb);
        g_processing_last_chroma_ms += (omp_get_wtime() - chroma_start) * 1000.0;
    }

    /* Grain (simple monochrome noise) generator - must be applied after denoiser */
    if( doGrain ) //Switch on/off
    {
        const double grain_start = omp_get_wtime();
        int strength = 50 * processing->grainStrength;
#pragma omp parallel for
        for( int i = 0; i < img_s; i+=3 )
        {
            uint32_t randomval = randomseed1 ^ ((i*randomseed2) * (randomseed3-i) * (i+randomseed4));
            int grain = ( randomval % strength ) - ( strength >> 2 ); //change value for strength

            if( processing->grainLumaWeight > 0 )
            {
                uint32_t sumL = outputImage[i+0] + outputImage[i+1] + outputImage[i+2];
                double weight = sumL / 1.5 / 65535.0;
                weight = ( weight * processing->grainLumaWeight / 100.0 ) + ( ( 100 - processing->grainLumaWeight ) / 100.0 );
                grain *= weight;
            }

            outputImage[i+0] = LIMIT16( outputImage[i+0] + grain );
            outputImage[i+1] = LIMIT16( outputImage[i+1] + grain );
            outputImage[i+2] = LIMIT16( outputImage[i+2] + grain );
        }
        g_processing_last_grain_ms = (omp_get_wtime() - grain_start) * 1000.0;
    }

    /* S4_processing capture: post processing core (matrix + levels + gamma +
     * curves + denoise + RBF + CA + sharpening + grain). RGB16 packed.
     * Inert when MLVAPP_PIPELINE_CAPTURE_DIR is unset. */
    if( mlv_pipeline_capture_should_capture_frame(frameIndex) )
    {
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = MLV_PIPELINE_STAGE_S4_PROCESSING;
        meta.format = MLV_PIPELINE_FORMAT_UINT16_RGB;
        meta.format_label = "uint16_rgb_post_processing";
        meta.width = imageX;
        meta.height = imageY;
        meta.bytes_per_line = imageX * 3 * (int)sizeof(uint16_t);
        meta.bytes_per_pixel = 3 * (int)sizeof(uint16_t);
        meta.channels = 3;
        meta.bit_depth = 16;
        meta.dual_iso_mode = NULL;
        meta.debayer_mode = NULL;
        meta.scaler = "none";
        meta.path_label = "applyProcessingObject";
        mlv_pipeline_capture(frameIndex, outputImage, &meta);
    }
}

/* Colour tonemap function for smooth gamut mapping */
static float Reinhard_for_colour(float x) { return (x < 0.5f) ? x : (ReinhardTonemap_f((x-0.5f)/0.5f)*0.5f+0.5f); }
static float Reinhard_for_blue(float x) { return (x < 0.7f) ? x : (ReinhardTonemap_f((x-0.7f)/0.3f)*0.3f+0.7f); }

static int processing_has_neutral_creative_adjustments(const processingObject_t * processing)
{
    if( !processing ) return 0;
    if( !processing->allow_creative_adjustments ) return 1;

    if( processing->hue_vs_luma_used
     || processing->hue_vs_saturation_used
     || processing->hue_vs_hue_used
     || processing->luma_vs_saturation_used )
    {
        return 0;
    }

    if( processing->vibrance > 1.01 || processing->vibrance < 0.99 ) return 0;
    if( processing->saturation > 1.01 || processing->saturation < 0.99 ) return 0;
    if( processing->toning_dry < 0.998f ) return 0;
    if( fabsf(processing->toning_wet[0]) > 0.0005f
     || fabsf(processing->toning_wet[1]) > 0.0005f
     || fabsf(processing->toning_wet[2]) > 0.0005f )
    {
        return 0;
    }

    return 1;
}

static void processing_capture_last_shadows_highlights_rbf_timing(void)
{
    recursive_bf_timing_t timing = { 0 };
    recursive_bf_get_last_timing(&timing);

    g_processing_last_shadows_highlights_rbf_total_ms = timing.total_ms;
    g_processing_last_shadows_highlights_rbf_boundary_ms = timing.boundary_ms;
    g_processing_last_shadows_highlights_rbf_range_table_ms = timing.range_table_ms;
    g_processing_last_shadows_highlights_rbf_left_ms = timing.left_ms;
    g_processing_last_shadows_highlights_rbf_right_ms = timing.right_ms;
    g_processing_last_shadows_highlights_rbf_horizontal_average_ms =
        timing.horizontal_average_ms;
    g_processing_last_shadows_highlights_rbf_vertical_down_ms =
        timing.vertical_down_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_first_line_ms =
        timing.vertical_up_first_line_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_ms =
        timing.vertical_up_body_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_diff_ms =
        timing.vertical_up_body_diff_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_ms =
        timing.vertical_up_body_store_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_factor_ms =
        timing.vertical_up_body_store_factor_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_ms =
        timing.vertical_up_body_store_color_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_src_ms =
        timing.vertical_up_body_store_color_src_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_prev_ms =
        timing.vertical_up_body_store_color_prev_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_assign_ms =
        timing.vertical_up_body_store_color_assign_ms;
    g_processing_last_shadows_highlights_rbf_vertical_up_ms =
        timing.vertical_up_ms;
    g_processing_last_shadows_highlights_rbf_output_ms = timing.output_ms;
}

static int processing_has_direct8_supported_creative_adjustments(const processingObject_t * processing)
{
    if( !processing ) return 0;
    if( !processing->allow_creative_adjustments ) return 1;

    if( processing->hue_vs_luma_used
     || processing->hue_vs_saturation_used
     || processing->hue_vs_hue_used
     || processing->luma_vs_saturation_used )
    {
        return 0;
    }

    if( processing->toning_dry < 0.998f ) return 0;
    if( fabsf(processing->toning_wet[0]) > 0.0005f
     || fabsf(processing->toning_wet[1]) > 0.0005f
     || fabsf(processing->toning_wet[2]) > 0.0005f )
    {
        return 0;
    }

    return 1;
}

static int processing_has_neutral_local_tone_adjustments(const processingObject_t * processing)
{
    if( !processing ) return 0;

    return fabs(processing->contrast) < 0.01
        && fabs(processing->clarity) < 0.01
        && fabs(processing->shadows_highlights.shadows) < 0.01
        && fabs(processing->shadows_highlights.highlights) < 0.01;
}

static int processing_has_direct8_supported_local_tone_adjustments(const processingObject_t * processing)
{
    if( !processing ) return 0;

    /* Round-4 item 2: SHARP preview uses the SAME contract as export/pause
     * now. The historical "broad pink wash" that forced preview to
     * neutral-only local tone dated to the era when preview hard-wired EVERY
     * receipt onto the AVX2 intrinsics kernel (see the removed preview
     * bypass in processing_direct8_requires_shared_kernel) while that kernel
     * skipped pre_calc_levels and tonemapped green with the blue Reinhard
     * curve - all fixed in round-4 items 0b/0c and pinned by the
     * DirectProcessed8* and PhaseE7/E8/E9 byte-identity families. Contrast
     * and shadows / highlights route to the shared C kernel in preview
     * exactly as they do for exports; only clarity has no direct8
     * implementation at all.
     *
     * Lane economics (when direct8 actually wins for a given clip/scale/
     * decode regime) are NOT this gate's job: the video layer decides that
     * in mlv_preview_direct8_input_is_cheap (video_mlv.c), where the input
     * path is known. This gate stays a pure receipt-capability contract. */
    return fabs(processing->clarity) < 0.01;
}

static int processing_has_direct8_shadow_highlight_adjustments(const processingObject_t * processing)
{
    if( !processing || !processing->allow_creative_adjustments ) return 0;

    /* Round-4 item 2: no preview exception - SH receipts are direct8-eligible
     * in preview now (gate above), so the blur pre-pass must run for them
     * exactly as it does for exports (shared helper, round-4 item 0c). */
    return fabs(processing->shadows_highlights.shadows) >= 0.01
        || fabs(processing->shadows_highlights.highlights) >= 0.01;
}

int processingHasShadowsHighlightsAdjustments(const processingObject_t * processing)
{
    return processing_has_direct8_shadow_highlight_adjustments(processing);
}

int processingGetShadowsHighlightsBlurData(const processingObject_t * processing,
                                           const uint16_t ** data,
                                           int * width,
                                           int * height,
                                           int * curveIndexMask)
{
    if( data ) *data = NULL;
    if( width ) *width = 0;
    if( height ) *height = 0;
    if( curveIndexMask ) *curveIndexMask = processing_shadows_highlights_curve_index_mask_enabled();

    if( !processing
     || !processingHasShadowsHighlightsAdjustments(processing)
     || !processing->shadows_highlights.blur_image )
    {
        return 0;
    }

    processing_buffer_t * buffer = processing->shadows_highlights.blur_image;
    if( !buffer->image || buffer->width == 0 || buffer->height == 0 )
    {
        return 0;
    }

    if( data ) *data = get_buffer(buffer);
    if( width ) *width = buffer->width;
    if( height ) *height = buffer->height;
    return 1;
}

/* A private part of the processing machine.
 *
 * Phase E7: AgX is now handled in the direct8 fast path (see
 * raw_processing_8bit_kernel.inc) so it no longer disqualifies the basic
 * matrix path. The gate above remains the same shape; the AgX clause is
 * intentionally removed because the kernel applies the forward AgX matrix
 * before the gamma LUT and the inverse AgX matrix after the creative
 * curves, byte-identical to the indirect path on neutral receipts.
 *
 * Note: this affects BOTH apply_processing_object's basic-matrix fast
 * branch (line 1228) AND processing_can_use_direct_8bit_output (which
 * gates the direct8 entry in applyProcessingObject8). The basic-matrix
 * fast branch in apply_processing_object does not currently handle AgX,
 * so we keep its AgX behaviour unchanged by re-checking AgX at that
 * use-site -- see the explicit AgX-in-fast-branch guard there. */
static int processing_can_use_basic_matrix_fast_path(const processingObject_t * processing)
{
    return processing->use_cam_matrix > 0
        && processing_has_neutral_creative_adjustments(processing)
        && processing_has_neutral_local_tone_adjustments(processing)
        && !processing->highlight_reconstruction
        && !processing->gradient_enable
        && processing->vignette_strength == 0;
}

static int processing_can_use_direct_8bit_output(const processingObject_t * processing)
{
    if( !processing ) return 0;

    return processing->use_cam_matrix > 0
        && processing_has_direct8_supported_creative_adjustments(processing)
        && processing_has_direct8_supported_local_tone_adjustments(processing)
        && !processing->highlight_reconstruction
        && !processing->gradient_enable
        && processing->vignette_strength == 0
        && !processing->lut_on
        && !processing->filter_on
        && processing->denoiserStrength <= 0
        && processing->rbfDenoiserLuma <= 0
        && processing->rbfDenoiserChroma <= 0
        && processing->ca_desaturate <= 0
        && !processingUsesChromaSeparation(processing)
        && processingGetSharpening(processing) <= 0.005
        && processing->grainStrength <= 0;
}

int processingCanUseDirect8BitOutput(const processingObject_t * processing)
{
    return processing_can_use_direct_8bit_output(processing);
}

static void processing_set_direct8_incompatibility_reason(const char * reason)
{
    if (!reason || !reason[0])
    {
        reason = "none";
    }
    snprintf(g_processing_last_direct8_incompatibility_reason,
             sizeof(g_processing_last_direct8_incompatibility_reason),
             "%s",
             reason);
}

const char * processingGetDirect8IncompatibilityReason(const processingObject_t * processing)
{
    if (!processing)
    {
        processing_set_direct8_incompatibility_reason("missing-processing-object");
        return g_processing_last_direct8_incompatibility_reason;
    }

    if (processing_can_use_direct_8bit_output(processing))
    {
        processing_set_direct8_incompatibility_reason("direct8-active");
        return g_processing_last_direct8_incompatibility_reason;
    }

    if (processing->use_cam_matrix <= 0)
    {
        processing_set_direct8_incompatibility_reason("cam-matrix-disabled");
        return g_processing_last_direct8_incompatibility_reason;
    }

    if (!processing_has_direct8_supported_creative_adjustments(processing))
    {
        if (processing->allow_creative_adjustments
            && (processing->hue_vs_luma_used
                || processing->hue_vs_saturation_used
                || processing->hue_vs_hue_used
                || processing->luma_vs_saturation_used))
        {
            processing_set_direct8_incompatibility_reason("creative-hue-vs-curve-enabled");
            return g_processing_last_direct8_incompatibility_reason;
        }
        if (processing->allow_creative_adjustments && processing->toning_dry < 0.998f)
        {
            processing_set_direct8_incompatibility_reason("creative-toning-enabled");
            return g_processing_last_direct8_incompatibility_reason;
        }
        if (processing->allow_creative_adjustments
            && (fabsf(processing->toning_wet[0]) > 0.0005f
                || fabsf(processing->toning_wet[1]) > 0.0005f
                || fabsf(processing->toning_wet[2]) > 0.0005f))
        {
            processing_set_direct8_incompatibility_reason("creative-toning-wet-enabled");
            return g_processing_last_direct8_incompatibility_reason;
        }
        processing_set_direct8_incompatibility_reason("creative-adjustments-unsupported");
        return g_processing_last_direct8_incompatibility_reason;
    }

    if (!processing_has_direct8_supported_local_tone_adjustments(processing))
    {
        if (processingPlaybackPreviewModeEnabled())
        {
            processing_set_direct8_incompatibility_reason("preview-local-tone-not-neutral");
        }
        else if (fabs(processing->clarity) >= 0.01)
        {
            processing_set_direct8_incompatibility_reason("clarity-enabled");
        }
        else
        {
            processing_set_direct8_incompatibility_reason("local-tone-unsupported");
        }
        return g_processing_last_direct8_incompatibility_reason;
    }

    if (processing->highlight_reconstruction)
    {
        processing_set_direct8_incompatibility_reason("highlight-reconstruction-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->gradient_enable)
    {
        processing_set_direct8_incompatibility_reason("gradient-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->vignette_strength != 0)
    {
        processing_set_direct8_incompatibility_reason("vignette-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->lut_on)
    {
        processing_set_direct8_incompatibility_reason("lut-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->filter_on)
    {
        processing_set_direct8_incompatibility_reason("filter-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->denoiserStrength > 0)
    {
        processing_set_direct8_incompatibility_reason("denoiser-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->rbfDenoiserLuma > 0 || processing->rbfDenoiserChroma > 0)
    {
        processing_set_direct8_incompatibility_reason("rbf-denoiser-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->ca_desaturate > 0)
    {
        processing_set_direct8_incompatibility_reason("ca-desaturate-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processingUsesChromaSeparation(processing))
    {
        processing_set_direct8_incompatibility_reason("chroma-separation-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processingGetSharpening(processing) > 0.005)
    {
        processing_set_direct8_incompatibility_reason("sharpening-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }
    if (processing->grainStrength > 0)
    {
        processing_set_direct8_incompatibility_reason("grain-enabled");
        return g_processing_last_direct8_incompatibility_reason;
    }

    processing_set_direct8_incompatibility_reason("direct8-inactive");
    return g_processing_last_direct8_incompatibility_reason;
}

static int processing_direct8_requires_shared_kernel(const processingObject_t * processing)
{
    /* Round-4 item 2: the preview bypass that returned 0 here routed EVERY
     * preview receipt onto the intrinsics kernel - including AgX, which the
     * intrin kernel does not implement at all (silently un-tonemapped
     * preview pixels on neutral AgX receipts). Preview now routes by kernel
     * capability exactly like export/pause. */
    return processing
        && ( processing->AgX
          || processing_has_direct8_shadow_highlight_adjustments(processing)
          || ( processing->allow_creative_adjustments
            && ( processing->vibrance <= 0.99
              || processing->vibrance >= 1.01
              || processing->saturation <= 0.99
              || processing->saturation >= 1.01 ) )
              || ( processing->allow_creative_adjustments
                && ( processing->contrast <= -0.01
                  || processing->contrast >= 0.01 ) ) );
}

/* Round-4 item 2: processing_direct8_preview_requires_serial_render is gone.
 * It forced threads=1 in preview for contrast / SH / vibrance / saturation
 * receipts - a 06976e82-era band-aid from the same firefight as the neutral-
 * only preview gate. Those receipts now route to the shared C kernel, which
 * already runs them row-partitioned and multi-threaded for exports with
 * byte-identity coverage. */

/*
 * Runtime-dispatched direct processed8 fast path.
 *
 * Three kernel variants compile here:
 *
 *   - apply_processing_object_8bit_fast_rows_scalar
 *       Plain C, baseline ISA. Reference for byte-identity comparisons.
 *
 *   - apply_processing_object_8bit_fast_rows_avx2
 *       Same source (raw_processing_8bit_kernel.inc) recompiled with
 *       __attribute__((target("avx2,fma"))) -- relies on GCC autovec to
 *       lift the matrix multiply / Y compute into AVX2 + FMA.
 *
 *   - apply_processing_object_8bit_fast_rows_avx2_intrin
 *       Hand-tuned AVX2 + FMA intrinsics
 *       (raw_processing_8bit_kernel_avx2_intrin.inc), 8 RGB pixels per
 *       inner iteration with explicit gathers + FMA. Phase 2A experiment.
 *
 * Dispatch policy (first call latches the function pointer via pthread_once):
 *
 *   - default: AVX2 autovec variant when CPU supports avx2+fma.
 *   - MLVAPP_DISABLE_AVX2=1: force scalar.
 *   - MLVAPP_ENABLE_AVX2_INTRIN_DIRECT8=1: force the intrinsics variant
 *     when AVX2+FMA is available.
 *   - MLVAPP_DISABLE_AVX2_INTRIN_DIRECT8=1: keep the autovec AVX2 variant
 *     when AVX2+FMA is available.
 *
 * Parity with scalar is enforced by the DirectProcessed8FastPath* tests in
 * tests/pipeline/test_dual_iso_pipeline.cpp. The intrinsics variant has its
 * own byte-identity test (DirectProcessed8FastPath_AVX2IntrinByteIdentity).
 */

typedef void (*apply_processing_object_8bit_fast_rows_fn_t)(
    processingObject_t *,
    int,
    int,
    int,
    uint16_t * __restrict,
    uint16_t * __restrict,
    uint8_t * __restrict,
    processing_direct8_kernel_timing_t *);

#define APPLY_8BIT_FAST_ROWS_NAME apply_processing_object_8bit_fast_rows_scalar
#include "raw_processing_8bit_kernel.inc"

#if defined(__GNUC__) && !defined(__clang__) && (defined(__x86_64__) || defined(__i386__))
#define MLVAPP_AVX2_DISPATCH_AVAILABLE 1
#define APPLY_8BIT_FAST_ROWS_NAME apply_processing_object_8bit_fast_rows_avx2
#define APPLY_8BIT_FAST_ROWS_ATTRS __attribute__((target("avx2,fma")))
#include "raw_processing_8bit_kernel.inc"

#define APPLY_8BIT_FAST_ROWS_NAME apply_processing_object_8bit_fast_rows_avx2_intrin
#define APPLY_8BIT_FAST_ROWS_ATTRS __attribute__((target("avx2,fma")))
#include "raw_processing_8bit_kernel_avx2_intrin.inc"
#endif

static pthread_once_t g_apply_processing_object_8bit_fast_rows_dispatch_once =
    PTHREAD_ONCE_INIT;
static apply_processing_object_8bit_fast_rows_fn_t
    g_apply_processing_object_8bit_fast_rows_fn = NULL;
static int g_apply_processing_object_8bit_fast_rows_use_avx2 = 0;
static int g_apply_processing_object_8bit_fast_rows_use_avx2_intrin = 0;

static int processing_profile_direct8_subloops_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if( !initialized )
    {
        enabled = processing_env_flag_enabled(getenv("MLVAPP_PROFILE_DIRECT8_SUBLOOPS"));
        initialized = 1;
    }
    return enabled;
}

static void apply_processing_object_8bit_fast_rows_dispatch_init(void)
{
    int use_avx2 = 0;
    int use_avx2_intrin = 0;
    const int disable_intrin =
        processing_env_flag_enabled(getenv("MLVAPP_DISABLE_AVX2_INTRIN_DIRECT8"));
#ifdef MLVAPP_AVX2_DISPATCH_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if( processing_env_flag_enabled(getenv("MLVAPP_DISABLE_AVX2")) )
    {
        use_avx2 = 0;
    }
    if( use_avx2 && !disable_intrin )
    {
        use_avx2_intrin = 1;
    }
    g_apply_processing_object_8bit_fast_rows_use_avx2 = use_avx2;
    g_apply_processing_object_8bit_fast_rows_use_avx2_intrin = use_avx2_intrin;
#ifdef MLVAPP_AVX2_DISPATCH_AVAILABLE
    if( use_avx2_intrin )
    {
        g_apply_processing_object_8bit_fast_rows_fn =
            apply_processing_object_8bit_fast_rows_avx2_intrin;
    }
    else if( use_avx2 )
    {
        g_apply_processing_object_8bit_fast_rows_fn =
            apply_processing_object_8bit_fast_rows_avx2;
    }
    else
    {
        g_apply_processing_object_8bit_fast_rows_fn =
            apply_processing_object_8bit_fast_rows_scalar;
    }
#else
    g_apply_processing_object_8bit_fast_rows_fn =
        apply_processing_object_8bit_fast_rows_scalar;
#endif
}

int processingFastPathAvx2Active(void)
{
    pthread_once(&g_apply_processing_object_8bit_fast_rows_dispatch_once,
                 apply_processing_object_8bit_fast_rows_dispatch_init);
    return g_apply_processing_object_8bit_fast_rows_use_avx2;
}

int processingFastPathAvx2IntrinActive(void)
{
    pthread_once(&g_apply_processing_object_8bit_fast_rows_dispatch_once,
                 apply_processing_object_8bit_fast_rows_dispatch_init);
    return g_apply_processing_object_8bit_fast_rows_use_avx2_intrin;
}

/* Test-only hook: re-run the dispatch policy from the current environment.
 * Production code latches once via pthread_once, but the tests run multiple
 * configs in a single process and need to flip the dispatch by editing the
 * env var. Returns 1 if the resulting selection points at the intrinsics
 * variant, 0 otherwise. Not declared in the public header. */
int processingFastPathReinitDispatchForTesting(void);
int processingFastPathReinitDispatchForTesting(void)
{
    /* Run the init at least once so the pthread_once flag is consumed and
     * future production callers don't re-run the env probe. After that,
     * directly mutate the latched globals from the current env. */
    pthread_once(&g_apply_processing_object_8bit_fast_rows_dispatch_once,
                 apply_processing_object_8bit_fast_rows_dispatch_init);
    apply_processing_object_8bit_fast_rows_dispatch_init();
    return g_apply_processing_object_8bit_fast_rows_use_avx2_intrin;
}

/* Test-only hook: invoke a specific direct8 fast-rows kernel variant directly,
 * bypassing the runtime dispatch, so parity tests can drive an arbitrary image
 * width (e.g. one that is NOT a multiple of 8) through a chosen variant's
 * scalar tail. variant: 0 = plain-C scalar, 1 = AVX2 autovec, 2 = AVX2
 * intrinsics. Returns 1 if the requested variant exists on this build and was
 * run, 0 otherwise (the AVX2 variants only compile on x86 GCC; callers must
 * still gate variants 1/2 on a host that advertises AVX2+FMA before running).
 * blurImage may be NULL (the kernel ignores it). Not declared in the public
 * header. */
int processingApplyDirect8FastRowsVariantForTesting(processingObject_t * processing,
                                                    int variant,
                                                    int imageX,
                                                    int rowStart,
                                                    int rowEnd,
                                                    uint16_t * inputImage,
                                                    uint16_t * blurImage,
                                                    uint8_t * outputImage);
int processingApplyDirect8FastRowsVariantForTesting(processingObject_t * processing,
                                                    int variant,
                                                    int imageX,
                                                    int rowStart,
                                                    int rowEnd,
                                                    uint16_t * inputImage,
                                                    uint16_t * blurImage,
                                                    uint8_t * outputImage)
{
    apply_processing_object_8bit_fast_rows_fn_t fn = NULL;
    if( variant == 0 )
    {
        fn = apply_processing_object_8bit_fast_rows_scalar;
    }
#ifdef MLVAPP_AVX2_DISPATCH_AVAILABLE
    else if( variant == 1 )
    {
        fn = apply_processing_object_8bit_fast_rows_avx2;
    }
    else if( variant == 2 )
    {
        fn = apply_processing_object_8bit_fast_rows_avx2_intrin;
    }
#endif
    if( fn == NULL )
    {
        return 0;
    }
    fn( processing, imageX, rowStart, rowEnd, inputImage, blurImage, outputImage, NULL );
    return 1;
}

static void apply_processing_object_8bit_fast_rows(processingObject_t * processing,
                                                   int imageX,
                                                   int rowStart,
                                                   int rowEnd,
                                                   uint16_t * __restrict inputImage,
                                                   uint16_t * __restrict blurImage,
                                                   uint8_t * __restrict outputImage,
                                                   processing_direct8_kernel_timing_t * timing)
{
    pthread_once(&g_apply_processing_object_8bit_fast_rows_dispatch_once,
                 apply_processing_object_8bit_fast_rows_dispatch_init);
    /* The hand-tuned AVX2 intrinsics variant handles only the neutral direct8
     * subset. Route AgX and local/creative tone cases through the shared C
     * kernel, where scalar and autovec stay byte-identical to the 16-bit
     * reference path. */
#ifdef MLVAPP_AVX2_DISPATCH_AVAILABLE
    if( processing_direct8_requires_shared_kernel(processing)
     && g_apply_processing_object_8bit_fast_rows_use_avx2_intrin )
    {
        if( g_apply_processing_object_8bit_fast_rows_use_avx2 )
        {
            apply_processing_object_8bit_fast_rows_avx2(processing,
                                                        imageX,
                                                        rowStart,
                                                        rowEnd,
                                                        inputImage,
                                                        blurImage,
                                                        outputImage,
                                                        timing);
        }
        else
        {
            apply_processing_object_8bit_fast_rows_scalar(processing,
                                                          imageX,
                                                          rowStart,
                                                          rowEnd,
                                                          inputImage,
                                                          blurImage,
                                                          outputImage,
                                                          timing);
        }
        return;
    }
#endif
    g_apply_processing_object_8bit_fast_rows_fn(processing,
                                                imageX,
                                                rowStart,
                                                rowEnd,
                                                inputImage,
                                                blurImage,
                                                outputImage,
                                                timing);
}

static void apply_processing_object_8bit_fast(processingObject_t * processing,
                                              int imageX,
                                              int imageY,
                                              uint16_t * __restrict inputImage,
                                              uint16_t * __restrict blurImage,
                                              uint8_t * __restrict outputImage,
                                              processing_direct8_kernel_timing_t * timing)
{
    apply_processing_object_8bit_fast_rows(processing,
                                           imageX,
                                           0,
                                           imageY,
                                           inputImage,
                                           blurImage,
                                           outputImage,
                                           timing);
}

void apply_processing_object( processingObject_t * processing,
                              int imageX, int imageY, 
                              uint16_t * __restrict inputImage, 
                              uint16_t * __restrict outputImage,
                              uint16_t * __restrict blurImage,
                              uint16_t * __restrict gradientMask,
                              float * __restrict vignetteMask,
                              processing_core_timing_t * core_timing )
{
    /* Number of elements */
    int img_s = imageX * imageY * 3;

    /* (for shorter code) */
    int32_t ** pm = processing->pre_calc_matrix;
    int32_t ** pmg = processing->pre_calc_matrix_gradient;
    uint16_t * img = inputImage;
    uint16_t * img_end = img + img_s;
    uint16_t * gm = gradientMask;
    float * vm = vignetteMask;
    float * vmpix = vm;

    /* Phase E7: processing_can_use_basic_matrix_fast_path no longer rules
     * out AgX (the direct8 kernel handles it). The 16-bit basic-matrix
     * branch below at the `if( use_basic_matrix_fast_path )` use-site does
     * not yet have an AgX code path, so we explicitly retain the previous
     * behaviour for the 16-bit path -- AgX still routes through the
     * generic per-pixel branch. */
    const int use_basic_matrix_fast_path =
        processing_can_use_basic_matrix_fast_path(processing) && !processing->AgX;

    /* For Y calculation */
    float rgb_to_Y[3] = {0.0f, 0.0f, 0.0f};
    if( use_basic_matrix_fast_path || ( processing->use_cam_matrix > 0 && !processing->exr_mode ) )
    {
        double inversemat[9];
        invertMatrix(colour_gamuts[processing->colour_gamut], inversemat);
        for (int i = 0; i < 3; ++i) rgb_to_Y[i] = inversemat[3+i];
    }

    double agx_inverse_matrix[9];
    if( processing->AgX )
    {
        invertMatrix(agx_compressed_matrix, agx_inverse_matrix);
    }

    /* In case of camera matrix */
    //double (* tone_mapping_function)(double) = tonemap_functions[processing->tonemap_function];

    const int capture_breakdown = core_timing != NULL;
    processing_core_timing_reset(core_timing);
    const double levels_start = capture_breakdown ? omp_get_wtime() : 0.0;
    /* Apply some precalcuolated settings */
    const uint16_t * pre_calc_levels = processing->pre_calc_levels;
    #pragma omp simd
    for (int i = 0; i < img_s; ++i)
    {
        /* Black + white level */
        img[i] = pre_calc_levels[ img[i] ];
    }
    if( capture_breakdown )
    {
        core_timing->levels_ms = (omp_get_wtime() - levels_start) * 1000.0;
    }

    const double color_start = capture_breakdown ? omp_get_wtime() : 0.0;
    const int blur_image_is_curve_index =
        processing_shadows_highlights_curve_index_mask_enabled();
    const int allow_creative_adjustments = processing->allow_creative_adjustments;
    const int use_cam_matrix = processing->use_cam_matrix > 0;
    const int exr_mode = processing->exr_mode;
    const int use_vignette = processing->vignette_strength != 0
        && vm
        && processing->vignette_end;
    const int use_highlight_reconstruction = processing->highlight_reconstruction;
    const int use_shadows_highlights =
        ( processing->shadows_highlights.shadows <= -0.01 || processing->shadows_highlights.shadows >= 0.01 )
     || ( processing->shadows_highlights.highlights <= -0.01 || processing->shadows_highlights.highlights >= 0.01 )
     || ( processing->clarity <= -0.01 || processing->clarity >= 0.01 );
    const int use_contrast =
        ( processing->contrast          <= -0.01 || processing->contrast          >= 0.01 )
     || ( processing->clarity           <= -0.01 || processing->clarity           >= 0.01 )
     || ( processing->gradient_contrast <= -0.01 || processing->gradient_contrast >= 0.01 );
    const int use_gradient_adjustments =
        ( processing->gradient_exposure_stops < -0.01 || processing->gradient_exposure_stops > 0.01 )
     || ( processing->gradient_contrast       < -0.01 || processing->gradient_contrast       > 0.01 );
    const double inv_65535 = 1.0 / 65535.0;
    const int pixel_count = img_s / 3;
    const double * const proper_wb = processing->proper_wb_matrix;
    const float rgb_to_y0 = rgb_to_Y[0];
    const float rgb_to_y1 = rgb_to_Y[1];
    const float rgb_to_y2 = rgb_to_Y[2];
    const double agx_m0 = agx_compressed_matrix[0];
    const double agx_m1 = agx_compressed_matrix[1];
    const double agx_m2 = agx_compressed_matrix[2];
    const double agx_m3 = agx_compressed_matrix[3];
    const double agx_m4 = agx_compressed_matrix[4];
    const double agx_m5 = agx_compressed_matrix[5];
    const double agx_m6 = agx_compressed_matrix[6];
    const double agx_m7 = agx_compressed_matrix[7];
    const double agx_m8 = agx_compressed_matrix[8];
    const double color_main_start = capture_breakdown ? omp_get_wtime() : 0.0;
    const int color_main_prelude_probe_mode = processing_main_prelude_probe_mode();
    const int color_main_prelude_wb_requested_mode = processing_main_prelude_wb_probe_mode();
    const int color_main_prelude_creative_requested_mode =
        processing_main_prelude_creative_probe_mode();
    const int color_main_prelude_probe_vignette =
        (color_main_prelude_probe_mode == 0 || color_main_prelude_probe_mode == 1);
    const int color_main_prelude_probe_creative =
        (color_main_prelude_probe_mode == 0 || color_main_prelude_probe_mode == 2
         || color_main_prelude_creative_requested_mode >= 0);
    const int color_main_prelude_probe_wb =
        (color_main_prelude_probe_mode == 0 || color_main_prelude_probe_mode == 3
         || color_main_prelude_wb_requested_mode >= 0);
    const int color_main_prelude_probe_any =
        (color_main_prelude_probe_vignette
         || color_main_prelude_probe_creative
         || color_main_prelude_probe_wb);
    const int color_main_prelude_wb_probe_mode =
        (color_main_prelude_wb_requested_mode >= 0)
            ? color_main_prelude_wb_requested_mode
            : (color_main_prelude_probe_wb ? 0 : -1);
    const int color_main_prelude_wb_probe_matrix =
        (color_main_prelude_wb_probe_mode == 0 || color_main_prelude_wb_probe_mode == 1);
    const int color_main_prelude_wb_probe_recon =
        (color_main_prelude_wb_probe_mode == 0 || color_main_prelude_wb_probe_mode == 2);
    const int color_main_prelude_creative_probe_mode =
        (color_main_prelude_creative_requested_mode >= 0)
            ? color_main_prelude_creative_requested_mode
            : (color_main_prelude_probe_creative ? 0 : -1);
    const int color_main_prelude_creative_probe_shadows =
        (color_main_prelude_creative_probe_mode == 0
         || color_main_prelude_creative_probe_mode == 1);
    const int color_main_prelude_creative_probe_contrast =
        (color_main_prelude_creative_probe_mode == 0
         || color_main_prelude_creative_probe_mode == 2);
    const int color_cam_wb_probe_mode = processing_cam_wb_probe_mode();
    const int color_cam_probe_any = color_cam_wb_probe_mode >= 0;
    const int color_cam_wb_probe_matrix =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 1);
    const int color_cam_wb_probe_gamut =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 2);
    const int color_cam_wb_probe_desat =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 3);
    const int color_cam_wb_probe_agx =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 4 || color_cam_wb_probe_mode == 5);
    const int color_cam_wb_probe_agx_clip_detail =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 5);
    const int color_cam_wb_probe_agx_matrix_detail =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 6);
    const int color_cam_wb_probe_agx_matrix_sat_detail =
        (color_cam_wb_probe_mode == 0 || color_cam_wb_probe_mode == 7);
    const int color_gamma_probe_mode = processing_cam_gamma_probe_mode();
    const int color_gamma_probe_any = color_gamma_probe_mode >= 0;
    const int color_gamma_probe_main =
        (color_gamma_probe_mode == 0 || color_gamma_probe_mode == 1);

    /* white balance & exposure & highlights & gamma & highlight reconstruction */
    if( use_basic_matrix_fast_path )
    {
        int32_t * pm0 = pm[0];
        int32_t * pm4 = pm[4];
        int32_t * pm8 = pm[8];
        uint16_t * pre_calc_gamma = processing->pre_calc_gamma;
        const float proper_wb_0 = (float)processing->proper_wb_matrix[0];
        const float proper_wb_1 = (float)processing->proper_wb_matrix[1];
        const float proper_wb_2 = (float)processing->proper_wb_matrix[2];
        const float proper_wb_3 = (float)processing->proper_wb_matrix[3];
        const float proper_wb_4 = (float)processing->proper_wb_matrix[4];
        const float proper_wb_5 = (float)processing->proper_wb_matrix[5];
        const float proper_wb_6 = (float)processing->proper_wb_matrix[6];
        const float proper_wb_7 = (float)processing->proper_wb_matrix[7];
        const float proper_wb_8 = (float)processing->proper_wb_matrix[8];
        const float rgb_to_y0 = rgb_to_Y[0];
        const float rgb_to_y1 = rgb_to_Y[1];
        const float rgb_to_y2 = rgb_to_Y[2];

        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            const float pix0 = (float)pm0[pix[0]];
            const float pix1 = (float)pm4[pix[1]];
            const float pix2 = (float)pm8[pix[2]];
            float result0 = pix0 * proper_wb_0 + pix1 * proper_wb_1 + pix2 * proper_wb_2;
            float result1 = pix0 * proper_wb_3 + pix1 * proper_wb_4 + pix2 * proper_wb_5;
            float result2 = pix0 * proper_wb_6 + pix1 * proper_wb_7 + pix2 * proper_wb_8;

            const float Y = rgb_to_y0 * result0
                          + rgb_to_y1 * result1
                          + rgb_to_y2 * result2;
            if( !processing->exr_mode && Y > 0.0f )
            {
                const float min_channel = MIN(MIN(result0, result1), result2);
                const float y_minus_min_channel = Y - min_channel;
                if( fabsf(y_minus_min_channel) > FLT_MIN )
                {
                    const float inv_y = 1.0f / Y;
                    const float result2_0 = Y - (Reinhard_for_colour((Y - result0) * inv_y) * Y);
                    /* Preserve the existing fast-path tone-map behavior for non-red channels. */
                    const float result2_1 = Y - (ReinhardTonemap_f((Y - result1) * inv_y) * Y);
                    const float result2_2 = Y - (Reinhard_for_blue((Y - result2) * inv_y) * Y);
                    const float desaturate_factor =
                        (Y - MIN(MIN(result2_0, result2_1), result2_2)) / y_minus_min_channel;

                    result0 = (result0 - Y) * desaturate_factor + Y;
                    result1 = (result1 - Y) * desaturate_factor + Y;
                    result2 = (result2 - Y) * desaturate_factor + Y;
                }
            }

            pix[0] = pre_calc_gamma[LIMIT16((uint32_t)result0)];
            pix[1] = pre_calc_gamma[LIMIT16((uint32_t)result1)];
            pix[2] = pre_calc_gamma[LIMIT16((uint32_t)result2)];
        }
        if( capture_breakdown )
        {
            core_timing->color_main_ms += (omp_get_wtime() - color_main_start) * 1000.0;
        }
    }
    else
    {
        #pragma omp simd
        for (int px = 0; px < pixel_count; ++px)
        {
            uint16_t * pix = img + (px * 3);
            uint16_t * bpix = blurImage + (px * 3);
            uint16_t * gmpix = gm ? gm + px : NULL;
            int32_t * pm0 = pm[0];
            int32_t * pm4 = pm[4];
            int32_t * pm8 = pm[8];
            int32_t * pmg0 = pmg[0];
            int32_t * pmg4 = pmg[4];
            int32_t * pmg8 = pmg[8];

            double expo_correction = 1.0;
            double expo_correction_gradient = 1.0;
            const double color_main_prelude_start =
                (capture_breakdown && color_main_prelude_probe_any) ? omp_get_wtime() : 0.0;
            const double color_main_prelude_vignette_start =
                (capture_breakdown && color_main_prelude_probe_vignette) ? omp_get_wtime() : 0.0;

            /* Vignette correction */
            if( use_vignette )
            {
                vmpix++;
                if( vmpix < processing->vignette_end )  /* just safety - sometimes parameters may change faster than processing */
                {
                    expo_correction *= pow( 1.0 + ( vmpix[0] * processing->vignette_strength / 128.0 ), 4 );
                }
            }
            if( capture_breakdown && color_main_prelude_probe_vignette )
            {
                core_timing->color_main_prelude_vignette_ms +=
                    (omp_get_wtime() - color_main_prelude_vignette_start) * 1000.0;
            }

            const double color_main_prelude_creative_start =
                (capture_breakdown && color_main_prelude_probe_creative) ? omp_get_wtime() : 0.0;
            if (allow_creative_adjustments)
            {
                /* shadows & highlights, clarity part 1 */
                const double color_main_prelude_creative_shadows_start =
                    (capture_breakdown && color_main_prelude_creative_probe_shadows) ? omp_get_wtime() : 0.0;
                if( use_shadows_highlights )
                {
                    int32_t bval;
                    if( blur_image_is_curve_index )
                    {
                        bval = bpix[0];
                    }
                    else
                    {
                        bval = ( ((pm0[bpix[0]] /* + pm[1][bpix[1]] + pm[2][bpix[2]] */) << 2)
                              + ((/* pm[3][bpix[0]] + */ pm4[bpix[1]] /* + pm[5][bpix[2]] */) * 11)
                              +  (/* pm[6][bpix[0]] + pm[7][bpix[1]] + */ pm8[bpix[2]]) ) >> 4;
                    }

                    if( processing->clarity <= -0.01 || processing->clarity >= 0.01 )
                    {
                        /* clarity part 1 */
                        double factor = processing->clarity_curve[LIMIT16(bval)];
                        expo_correction /= (factor * factor);
                    }
                    if( ( processing->shadows_highlights.shadows <= -0.01 || processing->shadows_highlights.shadows >= 0.01 )
                    || ( processing->shadows_highlights.highlights <= -0.01 || processing->shadows_highlights.highlights >= 0.01 ) )
                    {
                        /* highlight exposure factor */
                        expo_correction *= processing->shadows_highlights.shadow_highlight_curve[LIMIT16(bval)];
                    }
                }
                if( capture_breakdown && color_main_prelude_creative_probe_shadows )
                {
                    core_timing->color_main_prelude_creative_shadows_ms +=
                        (omp_get_wtime() - color_main_prelude_creative_shadows_start) * 1000.0;
                }

                /* Contrast on untouched pixel */
                const double color_main_prelude_creative_contrast_start =
                    (capture_breakdown && color_main_prelude_creative_probe_contrast) ? omp_get_wtime() : 0.0;
                if( use_contrast )
                {
                    int32_t cval = ( ((pm[0][pix[0]] /* + pm[1][pix[1]] + pm[2][pix[2]] */) << 2)
                                 + ((/* pm[3][pix[0]] + */ pm[4][pix[1]] /* + pm[5][pix[2]] */) * 11)
                                 +  (/* pm[6][pix[0]] + pm[7][pix[1]] + */ pm[8][pix[2]]) ) >> 4;

                    if( processing->clarity <= -0.01 || processing->clarity >= 0.01 )
                    {
                        /* clarity part 2 */
                        double factor = processing->clarity_curve[LIMIT16(cval)];
                        expo_correction *= factor * factor;
                    }
                    if( processing->contrast <= -0.01 || processing->contrast >= 0.01 )
                    {
                        /* contrast factor */
                        expo_correction *= processing->contrast_curve[LIMIT16(cval)];
                    }
                    if( processing->gradient_contrast <= -0.01 || processing->gradient_contrast >= 0.01 )
                    {
                        /* gradient contrast factor */
                        expo_correction_gradient *= processing->gradient_contrast_curve[LIMIT16(cval)];
                    }
                }
                if( capture_breakdown && color_main_prelude_creative_probe_contrast )
                {
                    core_timing->color_main_prelude_creative_contrast_ms +=
                        (omp_get_wtime() - color_main_prelude_creative_contrast_start) * 1000.0;
                }
            }
            if( capture_breakdown && color_main_prelude_probe_creative )
            {
                core_timing->color_main_prelude_creative_ms +=
                    (omp_get_wtime() - color_main_prelude_creative_start) * 1000.0;
            }

            const double color_main_prelude_wb_start =
                (capture_breakdown && color_main_prelude_probe_wb) ? omp_get_wtime() : 0.0;
            const double color_main_prelude_wb_matrix_start =
                (capture_breakdown && color_main_prelude_wb_probe_matrix) ? omp_get_wtime() : 0.0;
            const double color_main_prelude_wb_exposure_start =
                (capture_breakdown && color_main_prelude_probe_wb) ? omp_get_wtime() : 0.0;
            /* white balance & exposure */
            const double color_main_prelude_wb_matrix_r_start =
                (capture_breakdown && color_main_prelude_wb_probe_matrix) ? omp_get_wtime() : 0.0;
            const float wb_r = pm0[pix[0]];
            if( capture_breakdown && color_main_prelude_wb_probe_matrix )
            {
                core_timing->color_main_prelude_wb_matrix_r_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_matrix_r_start) * 1000.0;
            }
            const double color_main_prelude_wb_matrix_g_start =
                (capture_breakdown && color_main_prelude_wb_probe_matrix) ? omp_get_wtime() : 0.0;
            const float wb_g = pm4[pix[1]];
            if( capture_breakdown && color_main_prelude_wb_probe_matrix )
            {
                core_timing->color_main_prelude_wb_matrix_g_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_matrix_g_start) * 1000.0;
            }
            const double color_main_prelude_wb_matrix_b_start =
                (capture_breakdown && color_main_prelude_wb_probe_matrix) ? omp_get_wtime() : 0.0;
            const float wb_b = pm8[pix[2]];
            if( capture_breakdown && color_main_prelude_wb_probe_matrix )
            {
                core_timing->color_main_prelude_wb_matrix_b_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_matrix_b_start) * 1000.0;
            }
            float pix0 = wb_r * expo_correction;
            float pix1 = wb_g * expo_correction;
            float pix2 = wb_b * expo_correction;
            float tmp1 = wb_g;

            if( capture_breakdown && color_main_prelude_wb_probe_matrix )
            {
                core_timing->color_main_prelude_wb_matrix_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_matrix_start) * 1000.0;
            }
            if( capture_breakdown && color_main_prelude_probe_wb )
            {
                core_timing->color_main_prelude_wb_exposure_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_exposure_start) * 1000.0;
            }

            /* Gradient variables and part 1 */
            float pixg[3];
            if( processing->gradient_enable && gmpix && gmpix[0] != 0 && use_gradient_adjustments )
            {
                /* do the same for gradient as for the pic itself, but before the values are overwritten */
                /* white balance & exposure */
                const double color_main_prelude_wb_exposure_start =
                    (capture_breakdown && color_main_prelude_probe_wb) ? omp_get_wtime() : 0.0;
                const double color_main_prelude_wb_gradient_matrix_start =
                    (capture_breakdown && color_main_prelude_wb_probe_matrix) ? omp_get_wtime() : 0.0;
                const float wbg_r = pmg0[pix[0]];
                const float wbg_g = pmg4[pix[1]];
                const float wbg_b = pmg8[pix[2]];
                float pix0g = wbg_r * expo_correction * expo_correction_gradient;
                float pix1g = wbg_g * expo_correction * expo_correction_gradient;
                float pix2g = wbg_b * expo_correction * expo_correction_gradient;
                float tmp1g = wbg_g;

                pixg[0] = LIMIT16(pix0g);
                pixg[1] = LIMIT16(pix1g);
                pixg[2] = LIMIT16(pix2g);
                tmp1g   = LIMIT16(tmp1g);

                /* Now highlight reconstruction for gradient layer*/
                if (use_highlight_reconstruction)
                {
                    if(*processing->dual_iso != 0)
                    {
                        /* Check if its the range of highest green value possible */
                        /* the range makes it cleaner against pink noise */
                        if (tmp1g >= LIMIT16( processing->highest_green_gradient_diso - 5000 ) && tmp1g <= LIMIT16( processing->highest_green_gradient_diso + 5000 ))
                        {
                            if( pixg[1] < 1.1*pixg[0] && pixg[1] < pixg[2] )
                            {
                                pixg[1] = (pixg[0] + pixg[2]) / 2;
                            }
                        }
                    }
                    else
                    {
                        /* Check if its the highest green value possible */
                        if (tmp1g == processing->highest_green_gradient)
                        {
                            pixg[1] = (pixg[0] + pixg[2]) / 2;
                        }
                    }
                }
                if( capture_breakdown && color_main_prelude_wb_probe_matrix )
                {
                    core_timing->color_main_prelude_wb_gradient_matrix_ms +=
                        (omp_get_wtime() - color_main_prelude_wb_gradient_matrix_start) * 1000.0;
                }
                if( capture_breakdown && color_main_prelude_probe_wb )
                {
                    core_timing->color_main_prelude_wb_exposure_ms +=
                        (omp_get_wtime() - color_main_prelude_wb_exposure_start) * 1000.0;
                }
            }

            pix[0] = LIMIT16(pix0);
            pix[1] = LIMIT16(pix1);
            pix[2] = LIMIT16(pix2);
            tmp1   = LIMIT16(tmp1);

            const double color_main_prelude_wb_recon_start =
                (capture_breakdown && color_main_prelude_wb_probe_recon) ? omp_get_wtime() : 0.0;

            /* Now highlight reconstruction */
            if (use_highlight_reconstruction)
            {
                if(*processing->dual_iso != 0)
                {
                    /* Check if its the range of highest green value possible */
                    /* the range makes it cleaner against pink noise */
                    if (tmp1 >= LIMIT16( processing->highest_green_diso - 5000 ) && tmp1 <= LIMIT16( processing->highest_green_diso + 5000 ))
                    {
                        if( pix[1] < 1.1*pix[0] && pix[1] < pix[2] )
                        {
                            pix[1] = (pix[0] + pix[2]) / 2;
                        }
                    }
                }
            else
            {
                /* Check if its the highest green value possible */
                if (tmp1 == processing->highest_green)
                    {
                        pix[1] = (pix[0] + pix[2]) / 2;
                    }
                    /* Aggressive mode */
                    /*if (tmp1b >= processing->highest_green - 15000 && tmp1b <= processing->highest_green)
                    {
                        if( pix[1] < 1.1*pix[0] && pix[1] < pix[2] )
                        {
                            pix[1] = (pix[0] + pix[2]) / 2;
                        }
                    }*/
                }
            }
            if( capture_breakdown && color_main_prelude_wb_probe_recon )
            {
                core_timing->color_main_prelude_wb_recon_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_recon_start) * 1000.0;
            }
            if( capture_breakdown && color_main_prelude_probe_wb )
            {
                core_timing->color_main_prelude_wb_ms +=
                    (omp_get_wtime() - color_main_prelude_wb_start) * 1000.0;
            }

            if( capture_breakdown && color_main_prelude_probe_any )
            {
                core_timing->color_main_prelude_ms +=
                    (omp_get_wtime() - color_main_prelude_start) * 1000.0;
            }

            /* I really don't like how this if is in a big loop :(( */
            if( use_cam_matrix )
            {
                const double color_cam_start =
                    (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                const double color_cam_main_start =
                    (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                const double color_cam_wb_start =
                    (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                const double color_cam_wb_matrix_start =
                    (capture_breakdown && color_cam_wb_probe_matrix) ? omp_get_wtime() : 0.0;
                const double proper_wb_0 = proper_wb[0];
                const double proper_wb_1 = proper_wb[1];
                const double proper_wb_2 = proper_wb[2];
                const double proper_wb_3 = proper_wb[3];
                const double proper_wb_4 = proper_wb[4];
                const double proper_wb_5 = proper_wb[5];
                const double proper_wb_6 = proper_wb[6];
                const double proper_wb_7 = proper_wb[7];
                const double proper_wb_8 = proper_wb[8];
                /* WB correction */
                float pix0b = pix[0], pix1b = pix[1], pix2b = pix[2];
                float result[3];
                result[0] = pix0b * proper_wb_0 + pix1b * proper_wb_1 + pix2b * proper_wb_2;
                result[1] = pix0b * proper_wb_3 + pix1b * proper_wb_4 + pix2b * proper_wb_5;
                result[2] = pix0b * proper_wb_6 + pix1b * proper_wb_7 + pix2b * proper_wb_8;
                if( capture_breakdown && color_cam_wb_probe_matrix )
                {
                    core_timing->color_cam_wb_matrix_ms +=
                        (omp_get_wtime() - color_cam_wb_matrix_start) * 1000.0;
                }

                const double color_cam_wb_gamut_start =
                    (capture_breakdown && color_cam_wb_probe_gamut && !exr_mode) ? omp_get_wtime() : 0.0;
                const double color_cam_wb_desat_start =
                    (capture_breakdown && color_cam_wb_probe_desat && !exr_mode) ? omp_get_wtime() : 0.0;
                if (!exr_mode)
                {
                    /* Bring the colour back in to gamut by desaturating it, this will preserve hue and avoid ugliest clipping */
                    float Y = rgb_to_y0 * result[0]
                            + rgb_to_y1 * result[1]
                            + rgb_to_y2 * result[2];
                    float min_channel = MIN(MIN(result[0],result[1]),result[2]);

                    float result2[3];
                    for (int i = 0; i < 3; ++i) {
                        float Y_to_min_channel = (Y - result[i]) / Y;
                        float tonemapped;
                        if (i == 1)
                            tonemapped = ReinhardTonemap_f(Y_to_min_channel);
                        else if (i == 0)
                            tonemapped = Reinhard_for_colour(Y_to_min_channel);
                        else
                            tonemapped = Reinhard_for_blue(Y_to_min_channel);

                        result2[i] = -(tonemapped * Y)+Y;
                    }

                    float desaturate_factor = (Y - MIN(MIN(result2[0],result2[1]),result2[2])) / (Y - min_channel);

                    if (Y <= 0.0f) desaturate_factor = 1;

                    for (int i = 0; i < 3; ++i) result[i] = (result[i] - Y) * desaturate_factor + Y;
                }
                if( capture_breakdown && color_cam_wb_probe_gamut && !exr_mode )
                {
                    core_timing->color_cam_wb_gamut_ms +=
                        (omp_get_wtime() - color_cam_wb_gamut_start) * 1000.0;
                }
                if( capture_breakdown && color_cam_wb_probe_desat && !exr_mode )
                {
                    core_timing->color_cam_wb_desat_ms +=
                        (omp_get_wtime() - color_cam_wb_desat_start) * 1000.0;
                }
                if( capture_breakdown && color_cam_probe_any )
                {
                    core_timing->color_cam_wb_ms += (omp_get_wtime() - color_cam_wb_start) * 1000.0;
                }

                if (processing->AgX)
                {
                    const double color_cam_agx_start =
                        (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                    const double color_cam_agx_clip_start =
                        (capture_breakdown && color_cam_wb_probe_agx) ? omp_get_wtime() : 0.0;
                    // Clip. Just in case other footprint compression did not happen.
                    if( result[0] < 0.0 || result[1] < 0.0 || result[2] < 0.0 )
                    {
                        if( result[0] < 0.0 ) result[0] = 0.0;
                        if( result[1] < 0.0 ) result[1] = 0.0;
                        if( result[2] < 0.0 ) result[2] = 0.0;
                    }
                    if( capture_breakdown && color_cam_wb_probe_agx )
                    {
                        core_timing->color_cam_agx_clip_ms +=
                            (omp_get_wtime() - color_cam_agx_clip_start) * 1000.0;
                    }
                    const double color_cam_agx_matrix_start =
                        (capture_breakdown && color_cam_wb_probe_agx) ? omp_get_wtime() : 0.0;
                    // AgX compress chroma through matrix.
                    if( capture_breakdown && color_cam_wb_probe_agx_clip_detail )
                    {
                        if( result[0] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_r_count;
                        if( result[1] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_g_count;
                        if( result[2] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_b_count;
                    }
                    const double agx_out_r = result[0]*agx_m0+result[1]*agx_m1+result[2]*agx_m2;
                    const double color_cam_agx_matrix_r_start =
                        (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                    const double agx_out_g = result[0]*agx_m3+result[1]*agx_m4+result[2]*agx_m5;
                    const double color_cam_agx_matrix_g_start =
                        (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                    const double agx_out_b = result[0]*agx_m6+result[1]*agx_m7+result[2]*agx_m8;
                    const double color_cam_agx_matrix_b_start =
                        (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                    agx_store_u16_triplet_fast(agx_out_r, agx_out_g, agx_out_b, &pix[0], &pix[1], &pix[2]);
                    if( capture_breakdown && color_cam_wb_probe_agx_matrix_detail )
                    {
                        core_timing->color_cam_agx_matrix_r_ms +=
                            (omp_get_wtime() - color_cam_agx_matrix_r_start) * 1000.0;
                        core_timing->color_cam_agx_matrix_g_ms +=
                            (omp_get_wtime() - color_cam_agx_matrix_g_start) * 1000.0;
                        core_timing->color_cam_agx_matrix_b_ms +=
                            (omp_get_wtime() - color_cam_agx_matrix_b_start) * 1000.0;
                    }
                    if( capture_breakdown && color_cam_wb_probe_agx_matrix_sat_detail )
                    {
                        if( agx_out_r > 65535.0 ) ++core_timing->color_cam_agx_matrix_r_hi_count;
                        if( agx_out_g > 65535.0 ) ++core_timing->color_cam_agx_matrix_g_hi_count;
                        if( agx_out_b > 65535.0 ) ++core_timing->color_cam_agx_matrix_b_hi_count;
                    }
                    if( capture_breakdown && color_cam_wb_probe_agx )
                    {
                        core_timing->color_cam_agx_matrix_ms +=
                            (omp_get_wtime() - color_cam_agx_matrix_start) * 1000.0;
                    }
                    if( capture_breakdown && color_cam_probe_any )
                    {
                        core_timing->color_cam_agx_ms += (omp_get_wtime() - color_cam_agx_start) * 1000.0;
                    }
                }
                else
                {
                    pix[0] = LIMIT16(result[0]);
                    pix[1] = LIMIT16(result[1]);
                    pix[2] = LIMIT16(result[2]);
                }
                if( capture_breakdown && color_cam_probe_any )
                {
                    core_timing->color_cam_ms += (omp_get_wtime() - color_cam_start) * 1000.0;
                    core_timing->color_cam_main_ms += (omp_get_wtime() - color_cam_main_start) * 1000.0;
                }
            }

            /* Gamma and expo correction (shadows&highlights, contrast, clarity) */
            const double color_gamma_start =
                (capture_breakdown && color_gamma_probe_any) ? omp_get_wtime() : 0.0;
            const double color_gamma_main_start =
                (capture_breakdown && color_gamma_probe_main) ? omp_get_wtime() : 0.0;
            const uint16_t * const color_gamma_lut = processing->pre_calc_gamma;
            for( int i = 0; i < 3; i++ )
            {
                pix[i] = color_gamma_lut[ LIMIT16((uint32_t)pix[i]) ]; /* Not float-> int is done here */
            }
            if( capture_breakdown && color_gamma_probe_main )
            {
                core_timing->color_gamma_main_ms +=
                    (omp_get_wtime() - color_gamma_main_start) * 1000.0;
            }
            if( capture_breakdown && color_gamma_probe_any )
            {
                core_timing->color_gamma_ms += (omp_get_wtime() - color_gamma_start) * 1000.0;
            }

            /* Gradient part 2 & blending */
            const double color_gradient_start =
                (capture_breakdown && (color_cam_probe_any || color_gamma_probe_any))
                    ? omp_get_wtime() : 0.0;
            if( processing->gradient_enable && gmpix && gmpix[0] != 0 && use_gradient_adjustments )
            {
                /* WB correction gradient layer*/
                    if( use_cam_matrix )
                    {
                        const double color_cam_start =
                            (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                        const double color_cam_gradient_start =
                            (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                        const double color_cam_wb_start =
                            (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                    const double color_cam_wb_matrix_start =
                        (capture_breakdown && color_cam_wb_probe_matrix) ? omp_get_wtime() : 0.0;
                    const double proper_wb_0 = proper_wb[0];
                    const double proper_wb_1 = proper_wb[1];
                    const double proper_wb_2 = proper_wb[2];
                    const double proper_wb_3 = proper_wb[3];
                    const double proper_wb_4 = proper_wb[4];
                    const double proper_wb_5 = proper_wb[5];
                    const double proper_wb_6 = proper_wb[6];
                    const double proper_wb_7 = proper_wb[7];
                    const double proper_wb_8 = proper_wb[8];
                    float pix0b = pixg[0], pix1b = pixg[1], pix2b = pixg[2];
                    double result[3];
                    result[0] = pix0b * proper_wb_0 + pix1b * proper_wb_1 + pix2b * proper_wb_2;
                    result[1] = pix0b * proper_wb_3 + pix1b * proper_wb_4 + pix2b * proper_wb_5;
                    result[2] = pix0b * proper_wb_6 + pix1b * proper_wb_7 + pix2b * proper_wb_8;
                    if( capture_breakdown && color_cam_wb_probe_matrix )
                    {
                        core_timing->color_cam_wb_matrix_ms +=
                            (omp_get_wtime() - color_cam_wb_matrix_start) * 1000.0;
                    }
                    const double color_cam_wb_gamut_start =
                        (capture_breakdown && color_cam_wb_probe_gamut && !exr_mode) ? omp_get_wtime() : 0.0;
                    const double color_cam_wb_desat_start =
                        (capture_breakdown && color_cam_wb_probe_desat && !exr_mode) ? omp_get_wtime() : 0.0;
                    if (!exr_mode)
                    {
                        /* Bring the colour back in to gamut by desaturating it, this will preserve hue and avoid ugliest clipping */
                        float Y = rgb_to_y0 * result[0]
                                + rgb_to_y1 * result[1]
                                + rgb_to_y2 * result[2];
                        float min_channel = MIN(MIN(result[0],result[1]),result[2]);
                        float result2[3];
                        for (int i = 0; i < 3; ++i) {
                            float Y_to_min_channel = (Y - result[i]) / Y;
                            float tonemapped;
                            if (i == 1)
                                tonemapped = ReinhardTonemap_f(Y_to_min_channel);
                            else if (i == 0)
                                tonemapped = Reinhard_for_colour(Y_to_min_channel);
                            else
                                tonemapped = Reinhard_for_blue(Y_to_min_channel);
                            result2[i] = -(tonemapped * Y)+Y;
                        }
                        float desaturate_factor = (Y - MIN(MIN(result2[0],result2[1]),result2[2])) / (Y - min_channel);
                        if (Y <= 0.0f) desaturate_factor = 1;
                        for (int i = 0; i < 3; ++i) result[i] = (result[i] - Y) * desaturate_factor + Y; 
                    }
                    if( capture_breakdown && color_cam_wb_probe_gamut && !exr_mode )
                    {
                        core_timing->color_cam_wb_gamut_ms +=
                            (omp_get_wtime() - color_cam_wb_gamut_start) * 1000.0;
                    }
                    if( capture_breakdown && color_cam_wb_probe_desat && !exr_mode )
                    {
                        core_timing->color_cam_wb_desat_ms +=
                            (omp_get_wtime() - color_cam_wb_desat_start) * 1000.0;
                    }
                        if( capture_breakdown && color_cam_probe_any )
                        {
                            core_timing->color_cam_wb_ms += (omp_get_wtime() - color_cam_wb_start) * 1000.0;
                        }

                        /* obligatory code duplication */
                        if (processing->AgX)
                        {
                            const double color_cam_agx_start =
                                (capture_breakdown && color_cam_probe_any) ? omp_get_wtime() : 0.0;
                            const double color_cam_agx_clip_start =
                                (capture_breakdown && color_cam_wb_probe_agx) ? omp_get_wtime() : 0.0;
                        // Clip. Just in case other footprint compression did not happen.
                        if( result[0] < 0.0 || result[1] < 0.0 || result[2] < 0.0 )
                        {
                            if( result[0] < 0.0 ) result[0] = 0.0;
                            if( result[1] < 0.0 ) result[1] = 0.0;
                            if( result[2] < 0.0 ) result[2] = 0.0;
                        }
                        if( capture_breakdown && color_cam_wb_probe_agx )
                        {
                            core_timing->color_cam_agx_clip_ms +=
                                (omp_get_wtime() - color_cam_agx_clip_start) * 1000.0;
                        }
                        const double color_cam_agx_matrix_start =
                            (capture_breakdown && color_cam_wb_probe_agx) ? omp_get_wtime() : 0.0;
                        // AgX compress chroma through matrix.
                        if( capture_breakdown && color_cam_wb_probe_agx_clip_detail )
                        {
                            if( result[0] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_r_count;
                            if( result[1] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_g_count;
                            if( result[2] < 0.0 ) ++core_timing->color_cam_agx_clip_neg_b_count;
                        }
                        const double agx_out_r = result[0]*agx_m0+result[1]*agx_m1+result[2]*agx_m2;
                        const double color_cam_agx_matrix_r_start =
                            (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                        const double agx_out_g = result[0]*agx_m3+result[1]*agx_m4+result[2]*agx_m5;
                        const double color_cam_agx_matrix_g_start =
                            (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                        const double agx_out_b = result[0]*agx_m6+result[1]*agx_m7+result[2]*agx_m8;
                        const double color_cam_agx_matrix_b_start =
                            (capture_breakdown && color_cam_wb_probe_agx_matrix_detail) ? omp_get_wtime() : 0.0;
                        agx_store_float_triplet_fast(agx_out_r, agx_out_g, agx_out_b, &pixg[0], &pixg[1], &pixg[2]);
                        if( capture_breakdown && color_cam_wb_probe_agx_matrix_detail )
                        {
                            core_timing->color_cam_agx_matrix_r_ms +=
                                (omp_get_wtime() - color_cam_agx_matrix_r_start) * 1000.0;
                            core_timing->color_cam_agx_matrix_g_ms +=
                                (omp_get_wtime() - color_cam_agx_matrix_g_start) * 1000.0;
                            core_timing->color_cam_agx_matrix_b_ms +=
                                (omp_get_wtime() - color_cam_agx_matrix_b_start) * 1000.0;
                        }
                        if( capture_breakdown && color_cam_wb_probe_agx_matrix_sat_detail )
                        {
                            if( agx_out_r > 65535.0 ) ++core_timing->color_cam_agx_matrix_r_hi_count;
                            if( agx_out_g > 65535.0 ) ++core_timing->color_cam_agx_matrix_g_hi_count;
                            if( agx_out_b > 65535.0 ) ++core_timing->color_cam_agx_matrix_b_hi_count;
                        }
                        if( capture_breakdown && color_cam_wb_probe_agx )
                        {
                            core_timing->color_cam_agx_matrix_ms +=
                                (omp_get_wtime() - color_cam_agx_matrix_start) * 1000.0;
                        }
                        if( capture_breakdown && color_cam_probe_any )
                        {
                            core_timing->color_cam_agx_ms += (omp_get_wtime() - color_cam_agx_start) * 1000.0;
                        }
                    }
                    else
                    {
                        pixg[0] = LIMIT16(result[0]);
                        pixg[1] = LIMIT16(result[1]);
                        pixg[2] = LIMIT16(result[2]);
                    }
                    if( capture_breakdown && color_cam_probe_any )
                    {
                        core_timing->color_cam_ms += (omp_get_wtime() - color_cam_start) * 1000.0;
                        core_timing->color_cam_gradient_ms += (omp_get_wtime() - color_cam_gradient_start) * 1000.0;
                    }
                }

                /* Gamma and expo correction (shadows&highlights, contrast, clarity) gradient layer*/
                const double color_gamma_start =
                    (capture_breakdown && color_gamma_probe_any) ? omp_get_wtime() : 0.0;
                const double color_gamma_gradient_start =
                    (capture_breakdown && (color_gamma_probe_mode == 0 || color_gamma_probe_mode == 2))
                    ? omp_get_wtime() : 0.0;
                const uint16_t * const color_gamma_gradient_lut = processing->pre_calc_gamma_gradient;
                for( int i = 0; i < 3; i++ )
                {
                    pixg[i] = color_gamma_gradient_lut[ LIMIT16((uint32_t)pixg[i]) ];
                }
                if( capture_breakdown && (color_gamma_probe_mode == 0 || color_gamma_probe_mode == 2) )
                {
                    core_timing->color_gamma_gradient_ms +=
                        (omp_get_wtime() - color_gamma_gradient_start) * 1000.0;
                }
                if( capture_breakdown && color_gamma_probe_any )
                {
                    core_timing->color_gamma_ms += (omp_get_wtime() - color_gamma_start) * 1000.0;
                }

                /* Blending using the mask */
                const double blend = gmpix[0] * inv_65535;
                const double inv_blend = 1.0 - blend;
                pix[0] = blend * pixg[0] + inv_blend * pix[0];
                pix[1] = blend * pixg[1] + inv_blend * pix[1];
                pix[2] = blend * pixg[2] + inv_blend * pix[2];
            }
            if( capture_breakdown && (color_cam_probe_any || color_gamma_probe_any) )
            {
                core_timing->color_gradient_ms += (omp_get_wtime() - color_gradient_start) * 1000.0;
            }
        }
    }
    if( capture_breakdown )
    {
        core_timing->color_ms = (omp_get_wtime() - color_start) * 1000.0;
        core_timing->color_main_ms =
            MAX(core_timing->color_ms - core_timing->color_gradient_ms, 0.0);
    }

    const double creative_start = capture_breakdown ? omp_get_wtime() : 0.0;
    const int creative_probe_mode = processing_creative_probe_mode();
    const int creative_probe_hue_vs =
        (creative_probe_mode == 0 || creative_probe_mode == 1);
    const int creative_probe_vibrance =
        (creative_probe_mode == 0 || creative_probe_mode == 2);
    const int creative_probe_saturation =
        (creative_probe_mode == 0 || creative_probe_mode == 3);
    const int creative_probe_toning =
        (creative_probe_mode == 0 || creative_probe_mode == 4);
    const int creative_probe_curve =
        (creative_probe_mode == 0 || creative_probe_mode == 5);
    const int creative_probe_gradation =
        (creative_probe_mode == 0 || creative_probe_mode == 6);
    const int creative_probe_agx_inverse =
        (creative_probe_mode == 0 || creative_probe_mode == 7);
    //Code for HueVs...
    if( allow_creative_adjustments
     && ( processing->hue_vs_luma_used
       || processing->hue_vs_saturation_used
       || processing->hue_vs_hue_used
       || processing->luma_vs_saturation_used ) )
    {
        const double creative_hue_vs_start =
            (capture_breakdown && creative_probe_hue_vs) ? omp_get_wtime() : 0.0;
        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            float hsl[3];
            float rgb[3];
            for( int i = 0; i < 3; i++ ) rgb[i] = pix[i] / 65535.0f;
            fromRGBtoHSV( rgb, hsl );
            //rgb_to_hsl( pix, hsl );

            /* Calculate saturation value of untouched pixel (taken from vibrance, gives better results than from rgb_to_hsl) */
            // ///////////////////////
            double sat = 0;
            if( !( pix[0] == 0 && pix[1] == 0 && pix[2] == 0 ) )
            {
                uint16_t biggest = 0;
                uint16_t smallest = 65535;
                for( int i = 0; i < 3; i++ )
                {
                    if( pix[i] > biggest ) biggest = pix[i];
                    if( pix[i] < smallest ) smallest = pix[i];
                }
                sat = ((double)biggest - (double)smallest) / (double)biggest;
            }
            /* Some cheat factor to make the effect more visible */
            sat = 2.0 * sat / ( sat * sat + 1 );
            if( sat > 1.0 ) sat = 1.0;
            // ///////////////////////

            uint16_t hue = (uint16_t)(hsl[0] * 100.0);

            hsl[2] *= 1.0 + (processing->hue_vs_luma[hue] * sat * 2);
            if( hsl[2] < 0.0 ) hsl[2] = 0.0;
            //if( hsl[2] > 1.0 ) hsl[2] = 1.0;

            hsl[1] *= 1.0 + (processing->hue_vs_saturation[hue] * 2);
            if( hsl[1] < 0.0 ) hsl[1] = 0.0;
            //if( hsl[1] > 1.0 ) hsl[1] = 1.0;

            hsl[0] += 60 * processing->hue_vs_hue[hue];
            if( hsl[0] < 0 ) hsl[0] += 360;
            else if( hsl[0] >= 360 ) hsl[0] -= 360;

            uint16_t luma = (uint16_t)((hsl[2]) * 36000.0);
            hsl[1] *= 1.0 + (processing->luma_vs_saturation[luma] * 2);
            if( hsl[1] < 0.0 ) hsl[1] = 0.0;

            //hsl_to_rgb( hsl, pix );
            fromHSVtoRGB( hsl, rgb );
            for( int i = 0; i < 3; i++ ) pix[i] = LIMIT16( rgb[i] * 65535.0f + 0.5f );
        }
        if( capture_breakdown && creative_probe_hue_vs )
        {
            core_timing->creative_hue_vs_ms +=
                (omp_get_wtime() - creative_hue_vs_start) * 1000.0;
        }
    }

    if (allow_creative_adjustments)
    {
        if( processing->vibrance > 1.01 || processing->vibrance < 0.99 )
        {
            const double creative_vibrance_start =
                (capture_breakdown && creative_probe_vibrance) ? omp_get_wtime() : 0.0;
            const int creative_vibrance_positive = processing->vibrance > 1.0;
            const int32_t * const creative_vibrance_curve = processing->pre_calc_vibrance;
            /* Now vibrance, before saturation, because we need untouched colors (in terms of saturation) */
            for (uint16_t * pix = img; pix < img_end; pix += 3)
            {
                /* Pixel brightness = 4/16 R, 11/16 G, 1/16 blue; Try swapping the channels, it will look worse */
                const uint16_t r = pix[0];
                const uint16_t g = pix[1];
                const uint16_t b = pix[2];
                int32_t Y1 = ((r << 2) + (g * 11) + b) >> 4;
                int32_t Y2 = Y1 - 65536;

                /* Increase difference between channels and the saturation midpoint */
                int32_t pix0 = creative_vibrance_curve[r - Y2] + Y1;
                int32_t pix1 = creative_vibrance_curve[g - Y2] + Y1;
                int32_t pix2 = creative_vibrance_curve[b - Y2] + Y1;

                /* Positive vibrance in dependency to raw saturation */
                if( creative_vibrance_positive )
                {
                    /* Calculate saturation value of untouched pixel */
                    double sat = 0;
                    if( !( r == 0 && g == 0 && b == 0 ) )
                    {
                        uint16_t biggest = r > g ? r : g;
                        if( b > biggest ) biggest = b;
                        uint16_t smallest = r < g ? r : g;
                        if( b < smallest ) smallest = b;
                        sat = ((double)biggest - (double)smallest) / (double)biggest;
                    }
                    /* Some cheat factor to make the effect more visible */
                    sat = 2.0 * sat / ( sat * sat + 1 );
                    if( sat > 1.0 ) sat = 1.0;
                    /* The less saturated the pixel was, the more saturation it gets */
                    pix[0] = LIMIT16( r * sat + pix0 * ( 1.0 - sat ) );
                    pix[1] = LIMIT16( g * sat + pix1 * ( 1.0 - sat ) );
                    pix[2] = LIMIT16( b * sat + pix2 * ( 1.0 - sat ) );
                }
                /* Negative vibrance is the same as (un)saturation */
                else
                {
                    pix[0] = LIMIT16(pix0);
                    pix[1] = LIMIT16(pix1);
                    pix[2] = LIMIT16(pix2);
                }
            }
            if( capture_breakdown && creative_probe_vibrance )
            {
                core_timing->creative_vibrance_ms +=
                    (omp_get_wtime() - creative_vibrance_start) * 1000.0;
            }
        }

        if( processing->saturation > 1.01 || processing->saturation < 0.99 )
        {
            const double creative_saturation_start =
                (capture_breakdown && creative_probe_saturation) ? omp_get_wtime() : 0.0;
            /* Now saturation (looks way better after gamma) */
            for (uint16_t * pix = img; pix < img_end; pix += 3)
            {
                /* Pixel brightness = 4/16 R, 11/16 G, 1/16 blue; Try swapping the channels, it will look worse */
                int32_t Y1 = ((pix[0] << 2) + (pix[1] * 11) + pix[2]) >> 4;
                int32_t Y2 = Y1 - 65536;

                /* Increase difference between channels and the saturation midpoint */
                int32_t pix0 = processing->pre_calc_sat[pix[0] - Y2] + Y1;
                int32_t pix1 = processing->pre_calc_sat[pix[1] - Y2] + Y1;
                int32_t pix2 = processing->pre_calc_sat[pix[2] - Y2] + Y1;

                pix[0] = LIMIT16(pix0);
                pix[1] = LIMIT16(pix1);
                pix[2] = LIMIT16(pix2);
            }
            if( capture_breakdown && creative_probe_saturation )
            {
                core_timing->creative_saturation_ms +=
                    (omp_get_wtime() - creative_saturation_start) * 1000.0;
            }
        }
    }

    /* Toning */
    if (allow_creative_adjustments)
    {
        if( processing->toning_dry < 99.8 )
        {
            const double creative_toning_start =
                (capture_breakdown && creative_probe_toning) ? omp_get_wtime() : 0.0;
            for (uint16_t * pix = img; pix < img_end; pix += 3)
            {
                for( int i = 0; i < 3; i++ )
                {
                    pix[i] = pix[i] * processing->toning_dry + pix[i] * processing->toning_wet[i];
                }
            }
            if( capture_breakdown && creative_probe_toning )
            {
                core_timing->creative_toning_ms +=
                    (omp_get_wtime() - creative_toning_start) * 1000.0;
            }
        }
    }

    if (allow_creative_adjustments)
    {
        /* Contrast Curve (OMG putting this after gamma made it 999x better) */
        const double creative_curve_start =
            (capture_breakdown && creative_probe_curve) ? omp_get_wtime() : 0.0;
        const uint16_t * const creative_curve_lut = processing->pre_calc_curve_r;
        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            pix[0] = creative_curve_lut[ pix[0] ];
            pix[1] = creative_curve_lut[ pix[1] ];
            pix[2] = creative_curve_lut[ pix[2] ];
        }
        if( capture_breakdown && creative_probe_curve )
        {
            core_timing->creative_curve_ms +=
                (omp_get_wtime() - creative_curve_start) * 1000.0;
        }
    }

    if (allow_creative_adjustments)
    {
        //Gradation curve
        const double creative_gradation_start =
            (capture_breakdown && creative_probe_gradation) ? omp_get_wtime() : 0.0;
        const uint16_t * const creative_gradation_y_lut = processing->gcurve_y;
        const uint16_t * const creative_gradation_r_lut = processing->gcurve_r;
        const uint16_t * const creative_gradation_g_lut = processing->gcurve_g;
        const uint16_t * const creative_gradation_b_lut = processing->gcurve_b;
        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            pix[0] = creative_gradation_y_lut[ pix[0] ];
            pix[1] = creative_gradation_y_lut[ pix[1] ];
            pix[2] = creative_gradation_y_lut[ pix[2] ];
            pix[0] = creative_gradation_r_lut[ pix[0] ];
            pix[1] = creative_gradation_g_lut[ pix[1] ];
            pix[2] = creative_gradation_b_lut[ pix[2] ];
        }
        if( capture_breakdown && creative_probe_gradation )
        {
            core_timing->creative_gradation_ms +=
                (omp_get_wtime() - creative_gradation_start) * 1000.0;
        }
    }

    if (processing->AgX)
    {
        //Gradation curve
        const double creative_agx_inverse_start =
            (capture_breakdown && creative_probe_agx_inverse) ? omp_get_wtime() : 0.0;
        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            float as_float[3] = {pix[0], pix[1], pix[2]};
            double * m = agx_inverse_matrix;
            pix[0] = LIMIT16(as_float[0]*m[0]+as_float[1]*m[1]+as_float[2]*m[2]);
            pix[1] = LIMIT16(as_float[0]*m[3]+as_float[1]*m[4]+as_float[2]*m[5]);
            pix[2] = LIMIT16(as_float[0]*m[6]+as_float[1]*m[7]+as_float[2]*m[8]);
        }
        if( capture_breakdown && creative_probe_agx_inverse )
        {
            core_timing->creative_agx_inverse_ms +=
                (omp_get_wtime() - creative_agx_inverse_start) * 1000.0;
        }
    }
    if( capture_breakdown )
    {
        core_timing->creative_ms = (omp_get_wtime() - creative_start) * 1000.0;
    }

    const double output_start = capture_breakdown ? omp_get_wtime() : 0.0;
    memcpy(outputImage, inputImage, img_s * sizeof(uint16_t));

    if (processing->lut_on)
    {
        apply_lut( processing->lut, imageX, imageY, outputImage );
    }

    if (processing->filter_on)
    {
        applyFilterObject(processing->filter, imageX, imageY, outputImage);
    }
    if( capture_breakdown )
    {
        core_timing->output_ms = (omp_get_wtime() - output_start) * 1000.0;
    }
}

void applyProcessingObject8( processingObject_t * processing,
                             int imageX, int imageY,
                             uint16_t * __restrict inputImage,
                             uint8_t * __restrict outputImage,
                             int threads, int imageChanged, uint64_t frameIndex )
{
    (void)frameIndex;

    processingResetLastTimingTelemetry();

    if( !processing_can_use_direct_8bit_output(processing) )
    {
        return;
    }

    const double setup_start = omp_get_wtime();
    get_frame_transformed(processing, inputImage, imageX, imageY);
    g_processing_last_setup_ms = (omp_get_wtime() - setup_start) * 1000.0;

    if (imageChanged)
    {
        const double shadows_highlights_resize_start = omp_get_wtime();
        buffer_set_size(processing->shadows_highlights.blur_image, imageX, imageY);
        g_processing_last_shadows_highlights_resize_ms =
            (omp_get_wtime() - shadows_highlights_resize_start) * 1000.0;
    }

    const double shadows_highlights_start = omp_get_wtime();
    const int shadows_highlights_active =
        processing_has_direct8_shadow_highlight_adjustments(processing);
    if( shadows_highlights_active && imageChanged )
    {
        /* Round-4 item 0c: SAME pre-pass as the 16-bit pipeline (this path
         * used to keep a full-res-only copy of the policy, which drifted
         * when 25795f27 moved the 16-bit side to half-res - the PhaseE9
         * direct8 parity break). Also a perf win: even-dimension frames now
         * get the half-res RBF here too.
         * Round-4 item 6: PLAYBACK direct8 (preview mode on) uses the cheap
         * preview quarter-res blur lanes; EXPORTS (preview mode off) keep the
         * dimension-based export policy and stay full quality. See
         * processing_direct8_preview_quarterres_sh_enabled. */
        const int sh_export_policy =
            !( processingPlaybackPreviewModeEnabled()
               && processing_direct8_preview_quarterres_sh_enabled() );
        processing_compute_shadows_highlights_blur( processing, inputImage, imageX, imageY, threads, sh_export_policy );
    }
    g_processing_last_shadows_highlights_prep_ms = (omp_get_wtime() - shadows_highlights_start) * 1000.0;

    const double core_start = omp_get_wtime();
    g_processing_last_core_levels_ms = 0.0;

    const double color_start = omp_get_wtime();
    const int direct8_profile_subloops =
        (threads == 1) && processing_profile_direct8_subloops_enabled();
    processing_direct8_kernel_timing_t direct8_timing = { 0.0, 0.0, 0.0 };
    if( threads == 1 )
    {
        apply_processing_object_8bit_fast(processing,
                                          imageX,
                                          imageY,
                                          inputImage,
                                          shadows_highlights_active
                                              ? get_buffer(processing->shadows_highlights.blur_image)
                                              : NULL,
                                          outputImage,
                                          direct8_profile_subloops ? &direct8_timing : NULL);
    }
    else
    {
        #pragma omp parallel num_threads(threads) if(threads > 1)
        {
            const int thread_index = omp_get_thread_num();
            const int thread_count = omp_get_num_threads();
            const int row_start = ( imageY * thread_index ) / thread_count;
            const int row_end = ( imageY * ( thread_index + 1 ) ) / thread_count;
            if( row_end > row_start )
            {
                apply_processing_object_8bit_fast_rows(processing,
                                                       imageX,
                                                       row_start,
                                                       row_end,
                                                       inputImage,
                                                       shadows_highlights_active
                                                           ? get_buffer(processing->shadows_highlights.blur_image)
                                                           : NULL,
                                                       outputImage,
                                                       NULL);
            }
        }
    }

    if( direct8_profile_subloops )
    {
        g_processing_last_direct8_matrix_ms = direct8_timing.matrix_ms;
        g_processing_last_direct8_gamma_ms = direct8_timing.gamma_ms;
        g_processing_last_direct8_curves_ms = direct8_timing.curves_ms;
    }
    else
    {
        g_processing_last_direct8_matrix_ms = 0.0;
        g_processing_last_direct8_gamma_ms = 0.0;
        g_processing_last_direct8_curves_ms = 0.0;
    }
    g_processing_last_core_color_ms = (omp_get_wtime() - color_start) * 1000.0;
    g_processing_last_core_ms = (omp_get_wtime() - core_start) * 1000.0;
}

double processingGetLastSetupMilliseconds(void)
{
    return g_processing_last_setup_ms;
}

double processingGetLastShadowsHighlightsPrepMilliseconds(void)
{
    return g_processing_last_shadows_highlights_prep_ms;
}

double processingGetLastShadowsHighlightsResizeMilliseconds(void)
{
    return g_processing_last_shadows_highlights_resize_ms;
}

double processingGetLastShadowsHighlightsCopyMilliseconds(void)
{
    return g_processing_last_shadows_highlights_copy_ms;
}

double processingGetLastShadowsHighlightsFilterMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_ms;
}

double processingGetLastShadowsHighlightsFilterFullresMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_fullres_ms;
}

double processingGetLastShadowsHighlightsFilterHalfresDownsampleMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_halfres_downsample_ms;
}

double processingGetLastShadowsHighlightsFilterHalfresRbfMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_halfres_rbf_ms;
}

double processingGetLastShadowsHighlightsFilterHalfresUpsampleMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_halfres_upsample_ms;
}

double processingGetLastShadowsHighlightsFilterQuarterresDownsampleMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_quarterres_downsample_ms;
}

double processingGetLastShadowsHighlightsFilterQuarterresRbfMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_quarterres_rbf_ms;
}

double processingGetLastShadowsHighlightsFilterQuarterresUpsampleMilliseconds(void)
{
    return g_processing_last_shadows_highlights_filter_quarterres_upsample_ms;
}

int processingGetLastShadowsHighlightsQuarterresPathTakenForTesting(void)
{
    return g_processing_last_shadows_highlights_quarterres_path_taken;
}

int processingGetAggressiveX2ShadowsHighlightsQuarterresCacheForTesting(void)
{
    return g_processing_aggressive_x2_shadows_highlights_quarterres_cache;
}

int processingGetLastShadowsHighlightsQuarterresDownsampleCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_quarterres_downsample_completed;
}

int processingGetLastShadowsHighlightsQuarterresRbfCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_quarterres_rbf_completed;
}

int processingGetLastShadowsHighlightsQuarterresUpsampleCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_quarterres_upsample_completed;
}

int processingGetLastShadowsHighlightsHalfresDownsampleCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_halfres_downsample_completed;
}

int processingGetLastShadowsHighlightsHalfresRbfCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_halfres_rbf_completed;
}

int processingGetLastShadowsHighlightsHalfresUpsampleCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_halfres_upsample_completed;
}

int processingGetLastShadowsHighlightsFullresCompletedForTesting(void)
{
    return g_processing_last_shadows_highlights_fullres_completed;
}

int processingGetLastShadowsHighlightsPrepRunCountForTesting(void)
{
    return g_processing_last_shadows_highlights_prep_run_count;
}

int processingGetLastShadowsHighlightsFilterRunCountForTesting(void)
{
    return g_processing_last_shadows_highlights_filter_run_count;
}

double processingGetLastShadowsHighlightsRbfTotalMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_total_ms;
}

double processingGetLastShadowsHighlightsRbfBoundaryMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_boundary_ms;
}

double processingGetLastShadowsHighlightsRbfRangeTableMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_range_table_ms;
}

double processingGetLastShadowsHighlightsRbfLeftMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_left_ms;
}

double processingGetLastShadowsHighlightsRbfRightMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_right_ms;
}

double processingGetLastShadowsHighlightsRbfHorizontalAverageMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_horizontal_average_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalDownMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_down_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpFirstLineMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_first_line_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyDiffMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_diff_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreFactorMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_factor_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreColorMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreColorSrcMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_src_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreColorPrevMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_prev_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpBodyStoreColorAssignMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_body_store_color_assign_ms;
}

double processingGetLastShadowsHighlightsRbfVerticalUpMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_vertical_up_ms;
}

double processingGetLastShadowsHighlightsRbfOutputMilliseconds(void)
{
    return g_processing_last_shadows_highlights_rbf_output_ms;
}

double processingGetLastHighestGreenMilliseconds(void)
{
    return g_processing_last_highest_green_ms;
}

double processingGetLastCoreMilliseconds(void)
{
    return g_processing_last_core_ms;
}

double processingGetLastDenoiseMilliseconds(void)
{
    return g_processing_last_denoise_ms;
}

double processingGetLastRbfMilliseconds(void)
{
    return g_processing_last_rbf_ms;
}

double processingGetLastCaMilliseconds(void)
{
    return g_processing_last_ca_ms;
}

double processingGetLastChromaMilliseconds(void)
{
    return g_processing_last_chroma_ms;
}

double processingGetLastSharpenMilliseconds(void)
{
    return g_processing_last_sharpen_ms;
}

double processingGetLastGrainMilliseconds(void)
{
    return g_processing_last_grain_ms;
}

double processingGetLastCoreLevelsMilliseconds(void)
{
    return g_processing_last_core_levels_ms;
}

double processingGetLastCoreColorMilliseconds(void)
{
    return g_processing_last_core_color_ms;
}

double processingGetLastCoreColorMainMilliseconds(void)
{
    return g_processing_last_core_color_main_ms;
}

double processingGetLastCoreColorGradientMilliseconds(void)
{
    return g_processing_last_core_color_gradient_ms;
}

double processingGetLastCoreColorMainPreludeMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_ms;
}

double processingGetLastCoreColorMainPreludeVignetteMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_vignette_ms;
}

double processingGetLastCoreColorMainPreludeCreativeMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_creative_ms;
}

double processingGetLastCoreColorMainPreludeCreativeShadowsMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_creative_shadows_ms;
}

double processingGetLastCoreColorMainPreludeCreativeContrastMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_creative_contrast_ms;
}

double processingGetLastCoreColorMainPreludeWbMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_ms;
}

double processingGetLastCoreColorMainPreludeWbMatrixMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_matrix_ms;
}

double processingGetLastCoreColorMainPreludeWbMatrixRMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_matrix_r_ms;
}

double processingGetLastCoreColorMainPreludeWbMatrixGMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_matrix_g_ms;
}

double processingGetLastCoreColorMainPreludeWbMatrixBMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_matrix_b_ms;
}

double processingGetLastCoreColorMainPreludeWbGradientMatrixMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_gradient_matrix_ms;
}

double processingGetLastCoreColorMainPreludeWbExposureMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_exposure_ms;
}

double processingGetLastCoreColorMainPreludeWbGamutMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_gamut_ms;
}

double processingGetLastCoreColorMainPreludeWbReconMilliseconds(void)
{
    return g_processing_last_core_color_main_prelude_wb_recon_ms;
}

double processingGetLastCoreColorCamMilliseconds(void)
{
    return g_processing_last_core_color_cam_ms;
}

double processingGetLastCoreColorCamMainMilliseconds(void)
{
    return g_processing_last_core_color_cam_main_ms;
}

double processingGetLastCoreColorCamGradientMilliseconds(void)
{
    return g_processing_last_core_color_cam_gradient_ms;
}

double processingGetLastCoreColorCamWbMilliseconds(void)
{
    return g_processing_last_core_color_cam_wb_ms;
}

double processingGetLastCoreColorCamWbMatrixMilliseconds(void)
{
    return g_processing_last_core_color_cam_wb_matrix_ms;
}

double processingGetLastCoreColorCamWbGamutMilliseconds(void)
{
    return g_processing_last_core_color_cam_wb_gamut_ms;
}

double processingGetLastCoreColorCamWbDesatMilliseconds(void)
{
    return g_processing_last_core_color_cam_wb_desat_ms;
}

double processingGetLastCoreColorCamAgxMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_ms;
}

double processingGetLastCoreColorCamAgxClipMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_clip_ms;
}

double processingGetLastCoreColorCamAgxClipNegRCount(void)
{
    return g_processing_last_core_color_cam_agx_clip_neg_r_count;
}

double processingGetLastCoreColorCamAgxClipNegGCount(void)
{
    return g_processing_last_core_color_cam_agx_clip_neg_g_count;
}

double processingGetLastCoreColorCamAgxClipNegBCount(void)
{
    return g_processing_last_core_color_cam_agx_clip_neg_b_count;
}

double processingGetLastCoreColorCamAgxMatrixMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_matrix_ms;
}

double processingGetLastCoreColorCamAgxMatrixRMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_matrix_r_ms;
}

double processingGetLastCoreColorCamAgxMatrixGMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_matrix_g_ms;
}

double processingGetLastCoreColorCamAgxMatrixBMilliseconds(void)
{
    return g_processing_last_core_color_cam_agx_matrix_b_ms;
}

double processingGetLastCoreColorCamAgxMatrixRHiCount(void)
{
    return g_processing_last_core_color_cam_agx_matrix_r_hi_count;
}

double processingGetLastCoreColorCamAgxMatrixGHiCount(void)
{
    return g_processing_last_core_color_cam_agx_matrix_g_hi_count;
}

double processingGetLastCoreColorCamAgxMatrixBHiCount(void)
{
    return g_processing_last_core_color_cam_agx_matrix_b_hi_count;
}

double processingGetLastCoreColorGammaMilliseconds(void)
{
    return g_processing_last_core_color_gamma_ms;
}

double processingGetLastCoreColorGammaMainMilliseconds(void)
{
    return g_processing_last_core_color_gamma_main_ms;
}

double processingGetLastCoreColorGammaGradientMilliseconds(void)
{
    return g_processing_last_core_color_gamma_gradient_ms;
}

double processingGetLastCoreCreativeMilliseconds(void)
{
    return g_processing_last_core_creative_ms;
}

double processingGetLastCoreCreativeHueVsMilliseconds(void)
{
    return g_processing_last_core_creative_hue_vs_ms;
}

double processingGetLastCoreCreativeVibranceMilliseconds(void)
{
    return g_processing_last_core_creative_vibrance_ms;
}

double processingGetLastCoreCreativeSaturationMilliseconds(void)
{
    return g_processing_last_core_creative_saturation_ms;
}

double processingGetLastCoreCreativeToningMilliseconds(void)
{
    return g_processing_last_core_creative_toning_ms;
}

double processingGetLastCoreCreativeCurveMilliseconds(void)
{
    return g_processing_last_core_creative_curve_ms;
}

double processingGetLastCoreCreativeGradationMilliseconds(void)
{
    return g_processing_last_core_creative_gradation_ms;
}

double processingGetLastCoreCreativeAgxInverseMilliseconds(void)
{
    return g_processing_last_core_creative_agx_inverse_ms;
}

double processingGetLastCoreOutputMilliseconds(void)
{
    return g_processing_last_core_output_ms;
}

double processingGetLastDirect8MatrixMilliseconds(void)
{
    return g_processing_last_direct8_matrix_ms;
}

double processingGetLastDirect8GammaMilliseconds(void)
{
    return g_processing_last_direct8_gamma_ms;
}

double processingGetLastDirect8CurvesMilliseconds(void)
{
    return g_processing_last_direct8_curves_ms;
}

/* Pass frame buffer and do the transform on it */
void get_frame_transformed(processingObject_t *processing, uint16_t * frame_buf, uint16_t imageX, uint16_t imageY)
{
    if(processing->transformation == TR_ROT180)
    {
        int half_pixels = imageX * imageY / 2;
        size_t frame_size = (size_t)imageX * (size_t)imageY * sizeof(uint16_t) * 3u;

        uint8_t rgb_pixel[6];
        uint8_t * rgb_buf = (uint8_t*)frame_buf;
        for(int i = 0; i < half_pixels; ++i)
        {
            memcpy(rgb_pixel, rgb_buf + i*6, 6);
            memcpy(rgb_buf + i*6, rgb_buf + frame_size - 6 - i*6, 6);
            memcpy(rgb_buf + frame_size - 6 - i*6, rgb_pixel, 6);
        }
    }
}

/* Set contrast (S-curve really) */
void processingSetContrast( processingObject_t * processing, 
                            double DCRange,  /* Dark contrast range: 0.0 to 1.0 */
                            double DCFactor, /* Dark contrast strength: 0.0 to 8.0(any range really) */
                            double LCRange,  /* Light contrast range */
                            double LCFactor, /* Light contrast strength */
                            double lighten   /* 0-1 (for good highlight rolloff) */ )
{
    /* Basic things */
    processing->light_contrast_factor = LCFactor;
    processing->light_contrast_range = LCRange;
    processing->dark_contrast_factor = DCFactor;
    processing->dark_contrast_range = DCRange;
    processing->lighten = lighten;

    processing_update_curves(processing);
}

void processingSetDCRange(processingObject_t * processing, double DCRange)
{
    processing->dark_contrast_range = DCRange;
    processing_update_curves(processing);
}
void processingSetDCFactor(processingObject_t * processing, double DCFactor)
{
    processing->dark_contrast_factor = DCFactor;
    processing_update_curves(processing);
}
void processingSetLCRange(processingObject_t * processing, double LCRange) 
{
    processing->light_contrast_range = LCRange;
    processing_update_curves(processing);
}
void processingSetLCFactor(processingObject_t * processing, double LCFactor)
{
    processing->light_contrast_factor = LCFactor;
    processing_update_curves(processing);
}
void processingSetLightening(processingObject_t * processing, double lighten)
{
    processing->lighten = lighten;
    processing_update_curves(processing);
}

/* Have a guess what this does */
void processingSetExposureStops(processingObject_t * processing, double exposureStops)
{
    processing->exposure_stops = exposureStops;

    processingSetGamma(processing, processing->gamma_power);
    if( processing->gradient_enable != 0 ) processingSetGammaGradient(processing, processing->gamma_power);
    processing_update_matrices(processing);
    if( processing->gradient_enable != 0 ) processing_update_matrices_gradient(processing);
}

/* Have a guess what this does */
void processingSetGradientExposure(processingObject_t * processing, double value)
{
    processing->gradient_exposure_stops = value;

    processingSetGamma(processing, processing->gamma_power);
    processingSetGammaGradient(processing, processing->gamma_power);
    processing_update_matrices(processing);
    processing_update_matrices_gradient(processing);
}

/* Sets and precalculaes saturation */
void processingSetSaturation(processingObject_t * processing, double saturationFactor)
{
    processing->saturation = saturationFactor;

    /* Precaluclate for the algorithm */
    for (int i = 0; i < 131072; ++i)
    {
        double value = (i - 65536) * saturationFactor;
        processing->pre_calc_sat[i] = value;
    }
}


/* Sets and precalculaes vibrance */
void processingSetVibrance(processingObject_t *processing, double vibranceFactor)
{
    processing->vibrance = vibranceFactor;

    /* Precaluclate for the algorithm */
    for (int i = 0; i < 131072; ++i)
    {
        double value = (i - 65536) * vibranceFactor;
        processing->pre_calc_vibrance[i] = value;
    }
}


/* Set direction bias */
void processingSetSharpeningBias(processingObject_t * processing, double bias)
{
    processing->sharpen_bias = bias;
    /* Recalculates everythin */
    processingSetSharpening(processing, processingGetSharpening(processing));
}


void processingSetSharpening(processingObject_t * processing, double sharpen)
{
    processing->sharpen = sharpen;

    /* Anything more than ~0.5 just looks awful */
    sharpen = pow(sharpen, 1.5) * 0.55;

    double sharpen_x = sharpen * (1.0 - processing->sharpen_bias);
    double sharpen_y = sharpen * (1.0 + processing->sharpen_bias);
    double sharpen_a = 1.0 + (2.0 * sharpen_x) + (2.0 * sharpen_y);

    for (int i = 0; i < 65536; ++i)
    {
        processing->pre_calc_sharp_a[i] = (uint32_t)((double)i * sharpen_a);
        processing->pre_calc_sharp_x[i] = (uint16_t)LIMIT16((double)i * sharpen_x);
        processing->pre_calc_sharp_y[i] = (uint16_t)LIMIT16((double)i * sharpen_y);
    }
}

static double processing_receipt_tint_to_render_tint(double receipt_tint)
{
    const int negative = receipt_tint < 0.0;
    if(negative) receipt_tint = -receipt_tint;
    receipt_tint /= 10.0;
    receipt_tint = pow(receipt_tint, 1.75) * 10.0;
    return negative ? -receipt_tint : receipt_tint;
}

static void processing_wb_controls_to_multipliers(int kelvin, int receipt_tint,
                                                  double multipliers[3])
{
    get_kelvin_multipliers_rgb((double)kelvin, multipliers);
    /* Receipt/UI tint is stored as -100..100, while every production caller
     * passes receiptTint/10 to processingSetWhiteBalance. Model that exact
     * call boundary here so fitting cannot converge on a 10x stronger tint. */
    const double tint = processing_receipt_tint_to_render_tint(
        (double)receipt_tint / 10.0);
    multipliers[2] += tint / 11.0;
    multipliers[0] += tint / 19.0;
    const double lowest = MIN(MIN(multipliers[0], multipliers[1]), multipliers[2]);
    if(lowest > 0.0)
        for(int channel = 0; channel < 3; ++channel) multipliers[channel] /= lowest;
}

int processingWhiteBalanceControlsForAsShotNeutral(const double neutral[3],
                                                    int * wbTemp,
                                                    int * wbTint)
{
    if(!neutral || !wbTemp || !wbTint) return 0;
    double target[3];
    for(int channel = 0; channel < 3; ++channel)
    {
        if(!isfinite(neutral[channel]) || neutral[channel] <= 0.0) return 0;
        target[channel] = 1.0 / neutral[channel];
    }
    const double target_lowest = MIN(MIN(target[0], target[1]), target[2]);
    if(!isfinite(target_lowest) || target_lowest <= 0.0) return 0;
    for(int channel = 0; channel < 3; ++channel) target[channel] /= target_lowest;

    double best_error = DBL_MAX;
    int best_temperature = 6000;
    int best_tint = 0;
    /* Ten-kelvin resolution matches the precision useful to the integer GUI
     * receipt while keeping clip-open work bounded and deterministic. */
    for(int temperature = 2000; temperature <= 10000; temperature += 10)
    {
        for(int tint = -100; tint <= 100; ++tint)
        {
            double candidate[3];
            processing_wb_controls_to_multipliers(temperature, tint, candidate);
            if(candidate[0] <= 0.0 || candidate[1] <= 0.0 || candidate[2] <= 0.0)
                continue;
            double error = 0.0;
            for(int channel = 0; channel < 3; ++channel)
            {
                const double delta = log(candidate[channel] / target[channel]);
                error += delta * delta;
            }
            if(error < best_error)
            {
                best_error = error;
                best_temperature = temperature;
                best_tint = tint;
            }
        }
    }
    /* Refuse a finite but visibly wrong boundary-clamped approximation. The
     * squared log-ratio residual is scale-independent; 0.01 corresponds to
     * roughly a six-percent RMS channel-ratio error. */
    if(!isfinite(best_error) || best_error > 0.01) return 0;
    *wbTemp = best_temperature;
    *wbTint = best_tint;
    return 1;
}

/* Set white balance by kelvin + tint value */
void processingSetWhiteBalance(processingObject_t * processing, double WBKelvin, double WBTint)
{
    double * p_xyz_to_rgb;
    double * p_ciecam02;

    if( processing->use_cam_matrix == 2 ) {
        /* Danne matrix fix converts to "sRGB", so we will convert back to "XYZ",
         * pretending Danne fix is real sRGB, and THEN convert to whatever RGB
         * space we actually want. TODO: remove this as soon as possible */
        p_xyz_to_rgb = alloca(9 * sizeof(double)); // I LOVE ALLOCA OMG
        double sRGB_to_xyz[9];
        invertMatrix(xyz_to_rgb, sRGB_to_xyz); // the xyz_to_rgb is for sRGB
        double DanneEffectMatrix[9]; /* Applies the Danne effect to an image in XYZ space */
        multiplyMatrices(sRGB_to_xyz, xyz_to_rgb_danne, DanneEffectMatrix);
        multiplyMatrices(colour_gamuts[processing->colour_gamut], DanneEffectMatrix, p_xyz_to_rgb);
        p_ciecam02 = ciecam02_danne;
    }
    else {
        //scientific camera matrix
        p_xyz_to_rgb = colour_gamuts[processing->colour_gamut]/* alloca(9 * sizeof(double)) */;
        p_ciecam02 = ciecam02;
    }

    processing->kelvin = WBKelvin;

    /* Avoid changing tint if just changing kelvin */
    if (WBTint != processing->wb_tint)
    {
        /* Non-linear tint makes control finer in the middle */
        WBTint = processing_receipt_tint_to_render_tint(WBTint);

        processing->wb_tint = WBTint;
    }
    
    /* Calculate multipliers */

    /* double XYZ_White[3], XYZ_Temp[3], I[3] = {1,1,1};
    Kelvin_Daylight_to_XYZ(6500, XYZ_White);
    Kelvin_Daylight_to_XYZ(WBKelvin, XYZ_Temp);
    applyMatrix(XYZ_White, processing->cam_matrix);
    applyMatrix(XYZ_Temp, processing->cam_matrix);
    applyMatrix(I, processing->cam_matrix);
    for (int i = 0; i < 3; ++i)
    {
        processing->wb_multipliers[i] = XYZ_White[i]/(XYZ_Temp[i]*I[i]);
    } */

    get_kelvin_multipliers_rgb(WBKelvin, processing->wb_multipliers);

    /* Do tint (green and red channel seem to be main ones) */
    processing->wb_multipliers[2] += (WBTint / 11.0);
    processing->wb_multipliers[0] += (WBTint / 19.0);

    /* Make all channel multipliers be >= 1 */
    double lowest = MIN( MIN( processing->wb_multipliers[0], 
                              processing->wb_multipliers[1] ), 
                              processing->wb_multipliers[2] );

    for (int i = 0; i < 3; ++i) processing->wb_multipliers[i] /= lowest;

    /* White balance is part of the matrix */
    processing_update_matrices(processing);
    if( processing->gradient_enable != 0 ) processing_update_matrices_gradient(processing);


    /****************************** Now generate the matrix for scientific White Balance ******************************/

    double proper_wb_matrix[9] = {1,0,0,0,1,0,0,0,1};

    /* Get multipliers for this to undo what has been done, it was only done to do highlihgt reconstrucytion now */
    double multiplierz[3];
    get_kelvin_multipliers_rgb(WBKelvin, multiplierz); 

    /* Now create a matrix, which will take us back to raw colour by undoing
     * basic wb (which was useful for highlight reconstruction, also where tint was done) */
    double undo_basic_wb_matrix[9] = {
        1.0/multiplierz[0], 0, 0,
        0, 1.0/multiplierz[1], 0,
        0, 0, 1.0/multiplierz[2]
    };

    /* Get white points and convert to LMS space */
    double LMS_white[3];
    double LMS_temp[3];
    Kelvin_Daylight_to_XYZ(6500, LMS_white);
    Kelvin_Daylight_to_XYZ(WBKelvin, LMS_temp);
    applyMatrix(LMS_white, p_ciecam02);
    applyMatrix(LMS_temp, p_ciecam02);

    double LMS_multipliers[3];
    for (int i = 0; i < 3; ++i) LMS_multipliers[i] = LMS_white[i]/LMS_temp[i];

    double cam_to_xyz_D[9]; /* For daylight */
    double cam_to_xyz_A[9]; /* For tungsten (https://en.wikipedia.org/wiki/Standard_illuminant#Illuminant_A) */
    invertMatrix(processing->cam_matrix, cam_to_xyz_D);
    invertMatrix(processing->cam_matrix_A, cam_to_xyz_A);

    double cam_to_xyz_final[9];

    /* Blend the matrices between 3000 and 3600 Kelvin */
    // int mixfac = (WBKelvin-3000) / 600.0;
    // mixfac = MAX(MIN(1.0, mixfac), 0.0);
    // for (int i = 0; i < 9; ++i)
    // {
    //     cam_to_xyz_final[i] = cam_to_xyz_A[i]*(1.0-mixfac) + cam_to_xyz_D[i]*mixfac;
    // }
    // int mixfac = (WBKelvin-3000) / 3000;
    // mixfac = MAX(MIN(1.0, mixfac), 0.0);
    // mixfac = pow(mixfac, 0.6);
    // for (int i = 0; i < 9; ++i)
    // {
    //     cam_to_xyz_final[i] = cam_to_xyz_A[i]*(1.0-mixfac) + cam_to_xyz_D[i]*mixfac;
    // }
    // if (WBKelvin > 5000)
    /* TODO: fix this, only using daylight matrix right now as canon 50D colours went weird on luther samples
          ... See page 79 of DNG spec 1.4.0.0 - "One or Two Color Calibrations" */
        for (int i = 0; i < 9; ++i)
        {
            cam_to_xyz_final[i] = cam_to_xyz_D[i];
        }
    // else
        // for (int i = 0; i < 9; ++i)
        // {
        //     cam_to_xyz_final[i] = cam_to_xyz_A[i];
        // }

    multiplyMatrices(cam_to_xyz_final, undo_basic_wb_matrix, proper_wb_matrix);

    /* Convert to LMS space */
    double matrix_in_LMS[9];
    multiplyMatrices(p_ciecam02, proper_wb_matrix, matrix_in_LMS);

    /* Apply multipliers in LMS */
    for (int i = 0; i < 3; ++i)
    {
        for (int j = 0; j < 3; ++j)
        {
            matrix_in_LMS[i*3+j] = matrix_in_LMS[i*3+j] * LMS_multipliers[i];
        }
    }

    /* Matrix back to XYZ from LMS */
    double LMS_to_XYZ[9];
    invertMatrix(p_ciecam02, LMS_to_XYZ);

    double back_in_XYZ_matrix[9];
    multiplyMatrices(LMS_to_XYZ, matrix_in_LMS, back_in_XYZ_matrix);

    /* Back to sRGB (maybe something wider in future) */
    multiplyMatrices(p_xyz_to_rgb, back_in_XYZ_matrix, processing->proper_wb_matrix);
}

/* WB just by kelvin */
void processingSetWhiteBalanceKelvin(processingObject_t * processing, double WBKelvin)
{
    processingSetWhiteBalance( processing, WBKelvin,
                               processingGetWhiteBalanceTint(processing) );
}

/* WB tint */
void processingSetWhiteBalanceTint(processingObject_t * processing, double WBTint)
{
    processingSetWhiteBalance( processing, processingGetWhiteBalanceKelvin(processing),
                               WBTint );
}

/* Set gamma (Log-ing / tonemapping done here) */
void processingSetGamma(processingObject_t * processing, double gammaValue)
{
    processing->gamma_power = gammaValue;

    /* Needs to be inverse */
    //double gamma = 1.0 / gammaValue;

    /* Exposure */
    double exposure_factor = pow(2.0, processing->exposure_stops);
    if (processing->exposure_stops < 0) exposure_factor = 1;
    /* Precalculate the curve */
    for (int i = 0; i < 65536; ++i)
    {
        /* Tone mapping also (reinhard) */
        double pixel = (double)i/65535.0;
        pixel *= exposure_factor;
        processing->x_value = pixel;
        pixel = te_eval(processing->transfer_function) * 65535.0;
        pixel = LIMIT16(pixel);
        processing->pre_calc_gamma[i] = pixel;
    }

    /* So highlight reconstruction works */
    processing_update_highest_green(processing);
    processingSetGammaGradient(processing, gammaValue); /* Cool */
}

/* Set gamma for gradient image part (Log-ing / tonemapping done here) */
void processingSetGammaGradient(processingObject_t * processing, double gammaValue)
{
    processing->gamma_power = gammaValue;

    /* Needs to be inverse */
    //double gamma = 1.0 / gammaValue;

    /* Exposure */
    double exposure_factor = pow(2.0, processing->exposure_stops+processing->gradient_exposure_stops);
    if (processing->exposure_stops < 0) exposure_factor = 1;
    /* Precalculate the curve */
    for (int i = 0; i < 65536; ++i)
    {
        /* Tone mapping also (reinhard) */
        double pixel = (double)i/65535.0;
        pixel *= exposure_factor;
        processing->x_value = pixel;
        processing->pre_calc_gamma_gradient[i] = (uint16_t)LIMIT16(65535.0 * te_eval(processing->transfer_function));
    }

    /* So highlight reconstruction works */
    processing_update_highest_green_gradient(processing);
}

/* Range of saturation and hue is 0.0-1.0 */
void processingSet3WayCorrection( processingObject_t * processing,
                                  double highlightHue, double highlightSaturation,
                                  double midtoneHue, double midtoneSaturation,
                                  double shadowHue, double shadowSaturation )
{
    processing->highlight_hue = highlightHue;
    processing->highlight_sat = highlightSaturation;
    processing->midtone_hue = midtoneHue;
    processing->midtone_sat = midtoneSaturation;
    processing->shadow_hue = shadowHue;
    processing->shadow_sat = shadowSaturation;

    processing_update_curves(processing);
}

/* Set black and white level */
void processingSetBlackAndWhiteLevel(processingObject_t * processing,
                                      float mlvBlackLevel, int mlvWhiteLevel, int mlvBitDepth )
{
    /* Convert levels to 16bit */
    int bits_shift = 16 - mlvBitDepth;
    if( mlvBlackLevel >= 0 )
    {
        if(mlvBlackLevel) processing->black_level = mlvBlackLevel * pow( 2, bits_shift );
        else processing->black_level = 0;
    }
    if( mlvWhiteLevel >= 0 )
    {
        processing->white_level = mlvWhiteLevel << bits_shift;
        /* Lowering white level a bit avoids pink grain in highlihgt reconstruction */
        processing->white_level = (int)((double)(mlvWhiteLevel << bits_shift) * 0.993);
    }

    /* How much it needs to be stretched */
    double stretch = 65535.0 / (double)(processing->white_level - processing->black_level);

    for (int i = 0; i < 65536; ++i)
    {
        /* Stretch to the black-white level range */
        int new_value = (int)((double)(i - processing->black_level) * stretch);

        if (new_value < 65536 && new_value > 0)
        {
            processing->pre_calc_levels[i] = new_value;
        }
        else if (new_value <= 0)
        {
            processing->pre_calc_levels[i] = 0;
        }
        else if (new_value >= 65535)
        {
            processing->pre_calc_levels[i] = 65535;
        }
    }
}

/* Cheat functions */
void processingSetBlackLevel(processingObject_t * processing, float mlvBlackLevel, int mlvBitDepth)
{
    processingSetBlackAndWhiteLevel( processing,
                                     mlvBlackLevel,
                                     -1, // if -1 leave value untouched
                                     mlvBitDepth );
}
void processingSetWhiteLevel(processingObject_t * processing, int mlvWhiteLevel, int mlvBitDepth)
{
    processingSetBlackAndWhiteLevel( processing,
                                     -1, // if -1 leave value untouched
                                     mlvWhiteLevel,
                                     mlvBitDepth );
}

/* Set transformation */
void processingSetTransformation(processingObject_t * processing, int transformation)
{
    processing->transformation = transformation;
}

/* Decomissions a processing object completely(I hope) */
void freeProcessingObject(processingObject_t * processing)
{
    if(processing->gradient_mask) free(processing->gradient_mask);
    if(processing->vignette_mask) free(processing->vignette_mask);
    if(processing->sharpen_mask_gray) free(processing->sharpen_mask_gray);
    if(processing->sharpen_mask_sobel_h) free(processing->sharpen_mask_sobel_h);
    if(processing->sharpen_mask_sobel_v) free(processing->sharpen_mask_sobel_v);
    if(processing->sharpen_mask_contour) free(processing->sharpen_mask_contour);
    freeFilterObject(processing->filter);
    free_lut(processing->lut);
    for (int i = 8; i >= 0; --i) free(processing->pre_calc_matrix[i]);
    for (int i = 8; i >= 0; --i) free(processing->pre_calc_matrix_gradient[i]);
    for (int i = 6; i >= 0; --i) free(processing->cs_zone.pre_calc_rgb_to_YCbCr[i]);
    for (int i = 3; i >= 0; --i) free(processing->cs_zone.pre_calc_YCbCr_to_rgb[i]);
    free_image_buffer(processing->shadows_highlights.blur_image);
    free_image_buffer(processing->shadows_highlights.blur_image_half_in);
    free_image_buffer(processing->shadows_highlights.blur_image_half_out);
    denoise_2D_median_release(&processing->denoiser_context);
    free(processing);
}

static char * compile_ternary(char * function);

static int copy_lut_for_analysis(lut_t * dst, const lut_t * src)
{
    if (!dst || !src)
    {
        return 0;
    }

    float * new_cube = NULL;
    if (src->cube)
    {
        size_t values = (size_t)src->dimension * 3u;
        if (src->is3d)
        {
            values = (size_t)src->dimension
                   * (size_t)src->dimension
                   * (size_t)src->dimension
                   * 3u;
        }

        new_cube = malloc(values * sizeof(*new_cube));
        if (!new_cube)
        {
            return 1;
        }
        memcpy(new_cube, src->cube, values * sizeof(*new_cube));
    }

    float * dst_cube = dst->cube;
    *dst = *src;
    dst->cube = new_cube;
    if (dst_cube)
    {
        free(dst_cube);
    }

    return 0;
}

static int copy_transfer_function_for_analysis(processingObject_t * clone, const processingObject_t * src)
{
    if (!clone || !src || !src->transfer_function_string)
    {
        return 0;
    }

    char * transfer_function_string = malloc(strlen(src->transfer_function_string) + 1u);
    if (!transfer_function_string)
    {
        return 1;
    }
    strcpy(transfer_function_string, src->transfer_function_string);

    char * transfer_function_formatted = compile_ternary(src->transfer_function_string);
    if (!transfer_function_formatted)
    {
        free(transfer_function_string);
        return 1;
    }

    te_variable * var = &clone->x_variable;
    te_expr * transfer_function = te_compile(transfer_function_formatted, var, 1, NULL);
    if (!transfer_function)
    {
        free(transfer_function_formatted);
        free(transfer_function_string);
        return 1;
    }

    free(clone->transfer_function_string);
    free(clone->transfer_function_string_formatted);
    te_free(clone->transfer_function);

    clone->transfer_function_string = transfer_function_string;
    clone->transfer_function_string_formatted = transfer_function_formatted;
    clone->transfer_function = transfer_function;
    return 0;
}

static uint64_t processing_analysis_hash_bytes(uint64_t hash, const void * data, size_t bytes)
{
    const unsigned char * p = (const unsigned char *)data;

    if (!data || bytes == 0)
    {
        return hash;
    }

    for (size_t i = 0; i < bytes; ++i)
    {
        hash ^= (uint64_t)p[i];
        hash *= 1099511628211ULL;
    }

    return hash;
}

static size_t processing_lut_value_count_for_analysis(const lut_t * lut)
{
    if (!lut || !lut->cube || lut->dimension == 0)
    {
        return 0;
    }

    size_t values = (size_t)lut->dimension * 3u;
    if (lut->is3d)
    {
        values = (size_t)lut->dimension
               * (size_t)lut->dimension
               * (size_t)lut->dimension
               * 3u;
    }

    return values;
}

static uint64_t processing_analysis_source_fingerprint(const processingObject_t * processing)
{
    uint64_t hash = 1469598103934665603ULL;

    if (!processing)
    {
        return hash;
    }

    hash = processing_analysis_hash_bytes(hash, processing, sizeof(*processing));

    for (int i = 0; i < 9; ++i)
    {
        hash = processing_analysis_hash_bytes(hash,
                                              processing->pre_calc_matrix[i],
                                              65536u * sizeof(*processing->pre_calc_matrix[i]));
        hash = processing_analysis_hash_bytes(hash,
                                              processing->pre_calc_matrix_gradient[i],
                                              65536u * sizeof(*processing->pre_calc_matrix_gradient[i]));
    }

    for (int i = 0; i < 7; ++i)
    {
        hash = processing_analysis_hash_bytes(hash,
                                              processing->cs_zone.pre_calc_rgb_to_YCbCr[i],
                                              65536u * sizeof(*processing->cs_zone.pre_calc_rgb_to_YCbCr[i]));
    }

    for (int i = 0; i < 5; ++i)
    {
        hash = processing_analysis_hash_bytes(hash,
                                              processing->cs_zone.pre_calc_YCbCr_to_rgb[i],
                                              65536u * sizeof(*processing->cs_zone.pre_calc_YCbCr_to_rgb[i]));
    }

    hash = processing_analysis_hash_bytes(hash,
                                          processing->filter,
                                          processing->filter ? sizeof(*processing->filter) : 0u);
    hash = processing_analysis_hash_bytes(hash,
                                          processing->lut,
                                          processing->lut ? sizeof(*processing->lut) : 0u);
    if (processing->lut)
    {
        size_t lut_values = processing_lut_value_count_for_analysis(processing->lut);
        hash = processing_analysis_hash_bytes(hash,
                                              processing->lut->cube,
                                              lut_values * sizeof(*processing->lut->cube));
    }

    hash = processing_analysis_hash_bytes(hash,
                                          processing->transfer_function_string,
                                          processing->transfer_function_string
                                              ? strlen(processing->transfer_function_string) + 1u
                                              : 0u);
    hash = processing_analysis_hash_bytes(hash,
                                          processing->transfer_function_string_formatted,
                                          processing->transfer_function_string_formatted
                                              ? strlen(processing->transfer_function_string_formatted) + 1u
                                              : 0u);

    return hash;
}

static processingObject_t * processingCloneForAnalysisOnce(const processingObject_t * src)
{
    if (!src)
    {
        return NULL;
    }

    processingObject_t * clone = initProcessingObject();
    if (!clone)
    {
        return NULL;
    }

    filterObject_t * clone_filter = clone->filter;
    lut_t * clone_lut = clone->lut;
    int32_t * clone_pre_calc_matrix[9];
    int32_t * clone_pre_calc_matrix_gradient[9];
    int32_t * clone_pre_calc_rgb_to_YCbCr[7];
    int32_t * clone_pre_calc_YCbCr_to_rgb[5];
    processing_buffer_t * clone_blur_image = clone->shadows_highlights.blur_image;
    processing_buffer_t * clone_blur_image_half_in = clone->shadows_highlights.blur_image_half_in;
    processing_buffer_t * clone_blur_image_half_out = clone->shadows_highlights.blur_image_half_out;
    char * clone_transfer_function_string = clone->transfer_function_string;
    char * clone_transfer_function_string_formatted = clone->transfer_function_string_formatted;
    te_expr * clone_transfer_function = clone->transfer_function;

    for (int i = 0; i < 9; ++i)
    {
        clone_pre_calc_matrix[i] = clone->pre_calc_matrix[i];
        clone_pre_calc_matrix_gradient[i] = clone->pre_calc_matrix_gradient[i];
    }
    for (int i = 0; i < 7; ++i)
    {
        clone_pre_calc_rgb_to_YCbCr[i] = clone->cs_zone.pre_calc_rgb_to_YCbCr[i];
    }
    for (int i = 0; i < 5; ++i)
    {
        clone_pre_calc_YCbCr_to_rgb[i] = clone->cs_zone.pre_calc_YCbCr_to_rgb[i];
    }

    *clone = *src;

    clone->filter = clone_filter;
    clone->lut = clone_lut;
    if (src->filter && clone->filter)
    {
        filterObjectSetFilterStrength(clone->filter, src->filter->strength);
        filterObjectSetFilter(clone->filter, src->filter->filter_option);
    }
    if (copy_lut_for_analysis(clone->lut, src->lut))
    {
        processingFreeClone(clone);
        return NULL;
    }
    clone->shadows_highlights.blur_image = clone_blur_image;
    clone->shadows_highlights.blur_image_half_in = clone_blur_image_half_in;
    clone->shadows_highlights.blur_image_half_out = clone_blur_image_half_out;
    clone->gradient_mask = NULL;
    clone->vignette_mask = NULL;
    clone->vignette_end = NULL;
    clone->sharpen_mask_gray = NULL;
    clone->sharpen_mask_sobel_h = NULL;
    clone->sharpen_mask_sobel_v = NULL;
    clone->sharpen_mask_contour = NULL;
    clone->sharpen_mask_capacity = 0;
    clone->denoiser_context = NULL;
    clone->transfer_function_string = clone_transfer_function_string;
    clone->transfer_function_string_formatted = clone_transfer_function_string_formatted;
    clone->transfer_function = clone_transfer_function;
    clone->x_variable.address = &clone->x_value;
    clone->x_variable.name = "x";
    clone->x_variable.context = 0;
    clone->wbFindActive = 0;
    if (copy_transfer_function_for_analysis(clone, src))
    {
        processingFreeClone(clone);
        return NULL;
    }

    for (int i = 0; i < 9; ++i)
    {
        clone->pre_calc_matrix[i] = clone_pre_calc_matrix[i];
        clone->pre_calc_matrix_gradient[i] = clone_pre_calc_matrix_gradient[i];
        if (!clone->pre_calc_matrix[i] || !clone->pre_calc_matrix_gradient[i]
            || !src->pre_calc_matrix[i] || !src->pre_calc_matrix_gradient[i])
        {
            processingFreeClone(clone);
            return NULL;
        }
        memcpy(clone->pre_calc_matrix[i],
               src->pre_calc_matrix[i],
               65536u * sizeof(*clone->pre_calc_matrix[i]));
        memcpy(clone->pre_calc_matrix_gradient[i],
               src->pre_calc_matrix_gradient[i],
               65536u * sizeof(*clone->pre_calc_matrix_gradient[i]));
    }

    for (int i = 0; i < 7; ++i)
    {
        clone->cs_zone.pre_calc_rgb_to_YCbCr[i] = clone_pre_calc_rgb_to_YCbCr[i];
        if (clone->cs_zone.pre_calc_rgb_to_YCbCr[i] && src->cs_zone.pre_calc_rgb_to_YCbCr[i])
        {
            memcpy(clone->cs_zone.pre_calc_rgb_to_YCbCr[i],
                   src->cs_zone.pre_calc_rgb_to_YCbCr[i],
                   65536u * sizeof(*clone->cs_zone.pre_calc_rgb_to_YCbCr[i]));
        }
    }
    for (int i = 0; i < 5; ++i)
    {
        clone->cs_zone.pre_calc_YCbCr_to_rgb[i] = clone_pre_calc_YCbCr_to_rgb[i];
        if (clone->cs_zone.pre_calc_YCbCr_to_rgb[i] && src->cs_zone.pre_calc_YCbCr_to_rgb[i])
        {
            memcpy(clone->cs_zone.pre_calc_YCbCr_to_rgb[i],
                   src->cs_zone.pre_calc_YCbCr_to_rgb[i],
                   65536u * sizeof(*clone->cs_zone.pre_calc_YCbCr_to_rgb[i]));
        }
    }

    return clone;
}

processingObject_t * processingCloneForAnalysis(const processingObject_t * src)
{
    enum { MAX_ANALYSIS_CLONE_ATTEMPTS = 16 };

    for (int attempt = 0; attempt < MAX_ANALYSIS_CLONE_ATTEMPTS; ++attempt)
    {
        uint64_t before = processing_analysis_source_fingerprint(src);
        processingObject_t * clone = processingCloneForAnalysisOnce(src);
        uint64_t after = processing_analysis_source_fingerprint(src);

        if (!clone)
        {
            return NULL;
        }

        if (before == after)
        {
            return clone;
        }

        processingFreeClone(clone);
    }

    return NULL;
}

void processingFreeClone(processingObject_t * processing)
{
    if (!processing)
    {
        return;
    }
    if (processing->transfer_function_string)
    {
        free(processing->transfer_function_string);
        processing->transfer_function_string = NULL;
    }
    if (processing->transfer_function_string_formatted)
    {
        free(processing->transfer_function_string_formatted);
        processing->transfer_function_string_formatted = NULL;
    }
    if (processing->transfer_function)
    {
        te_free(processing->transfer_function);
        processing->transfer_function = NULL;
    }
    freeProcessingObject(processing);
}

/* Find correct white balance setting for one selected pixel */
void processingFindWhiteBalance(processingObject_t *processing, int imageX, int imageY, uint16_t *inputImage, int posX, int posY, int *wbTemp, int *wbTint, int mode)
{
    const char *wbValueTraceEnv = getenv("MLVAPP_LOOK_ASSIST_WB_VALUE_TRACE");
    const int wbValueTrace = wbValueTraceEnv
                           && wbValueTraceEnv[0] != '\0'
                           && strcmp(wbValueTraceEnv, "0") != 0;

    /* Number of elements */
    int img_s = imageX * imageY * 3;

    /* (for shorter code) */
    int32_t ** pm = processing->pre_calc_matrix;
    uint16_t * img = inputImage;

    /* Apply some precalcuolated settings */
    for (int i = 0; i < img_s; ++i)
    {
        /* Black + white level */
        img[i] = processing->pre_calc_levels[ img[i] ];
    }

    double oriTemp = processing->kelvin;
    double oriTint = processing->wb_tint;
    int nearestTemp;
    int nearestTint;
    uint32_t deltaMin = UINT32_MAX;

    /* this is the pixel for what we search the parameters */
    uint32_t pixR = 0;
    uint32_t pixG = 0;
    uint32_t pixB = 0;
    uint32_t counter = 0;

    /* average with neighbor pixels */
    for( int y = -10; y <= 10; y++ )
    {
        for( int x = -10; x <= 10; x++ )
        {
            if( posX+x < 0 || posY+y < 0 || posX+x >= imageX || posY+y >= imageY ) continue;

            uint16_t * pix = img + ( ( (posY+y) * imageX + (posX+x) ) * 3 );

            pixR += pix[0];
            pixG += pix[1];
            pixB += pix[2];
            counter++;
        }
    }
    pixR /= counter;
    pixG /= counter;
    pixB /= counter;

    const uint32_t patchMeanR = pixR;
    const uint32_t patchMeanG = pixG;
    const uint32_t patchMeanB = pixB;

    /* skin wb mode */
    if( mode == 1 )
    {
        pixR *= 1.0 / (4686.0 / 6069 * 1.979);
        pixB *= 1.0 / (2817.0 / 6069 * 1.678);
    }

    /* activate quick matrix build (we need matrix just for this RGB values) */
    processing->wbR = pixR;
    processing->wbG = pixG;
    processing->wbB = pixB;
    processing->wbFindActive = 1;

    /* Trail & Error :-P */
    for( int temp = 2300; temp <= 10000; temp += 10 )
    {
        for( int tint = -100; tint <= 100; tint += 1 )
        {
            processingSetWhiteBalance( processing, temp, tint/10.0 );

            /* --- maybe this can also be exchanged by apply_processing_object, but here it is simplified and hopefully faster --- */
            /* white balance & exposure */
            int32_t pix0 = LIMIT16(pm[0][pixR] /*+ pm[1][pixG] + pm[2][pixB]*/);
            int32_t pix1 = LIMIT16(/*pm[3][pixR] +*/ pm[4][pixG] /*+ pm[5][pixB]*/);
            int32_t pix2 = LIMIT16(/*pm[6][pixR] + pm[7][pixG] +*/ pm[8][pixB]);

            /* standard highlight reconstruction */
            if( processing->highlight_reconstruction && pix1 == processing->highest_green )
            {
                pix1 = ( pix0 + pix2 ) / 2;
            }
            /* --- */

            if( processing->use_cam_matrix > 0 )
            {
                uint16_t pix0b = pix0, pix1b = pix1, pix2b = pix2;
                double result[3];
                result[0] = pix0b * processing->proper_wb_matrix[0] + pix1b * processing->proper_wb_matrix[1] + pix2b * processing->proper_wb_matrix[2];
                result[1] = pix0b * processing->proper_wb_matrix[3] + pix1b * processing->proper_wb_matrix[4] + pix2b * processing->proper_wb_matrix[5];
                result[2] = pix0b * processing->proper_wb_matrix[6] + pix1b * processing->proper_wb_matrix[7] + pix2b * processing->proper_wb_matrix[8];
                pix0 = LIMIT16(result[0]);
                pix1 = LIMIT16(result[1]);
                pix2 = LIMIT16(result[2]);
            }
            pix0 = processing->pre_calc_gamma[ pix0 ];
            pix1 = processing->pre_calc_gamma[ pix1 ];
            pix2 = processing->pre_calc_gamma[ pix2 ];

            /* for neutral grey all 3 are the same, so searching the min delta */
            uint32_t delta = abs( pix0 - pix1 ) + abs( pix1 - pix2 ) + abs( pix0 - pix2 );

            if( delta < deltaMin )
            {
                nearestTemp = temp;
                nearestTint = tint;
                deltaMin = delta;
            }

            /* delta won't be smaller than 0 */
            if( deltaMin == 0 ) break;
        }

        /* delta won't be smaller than 0 */
        if( deltaMin == 0 ) break;
    }

    /* deactivate quick matrix build */
    processing->wbFindActive = 0;

    /* set it back to where we began */
    processingSetWhiteBalance( processing, (double)oriTemp, (double)oriTint );

    if( wbValueTrace )
    {
        fprintf( stderr,
                 "WB_VALUE_TRACE_PROCESSING image=%dx%d pos=%d/%d mode=%d patch_count=%u "
                 "black_level=%.3f white_level=%d level_range=%.3f "
                 "post_levels_patch_mean_rgb=%u/%u/%u solver_rgb=%u/%u/%u "
                 "candidate_temp=%d candidate_tint=%d original_temp=%.0f original_tint=%.3f\n",
                 imageX, imageY, posX, posY, mode, counter,
                 (double)processing->black_level,
                 processing->white_level,
                 (double)processing->white_level - (double)processing->black_level,
                 patchMeanR, patchMeanG, patchMeanB,
                 pixR, pixG, pixB,
                 nearestTemp, nearestTint,
                 oriTemp, oriTint );
        fflush( stderr );
    }

    /* give the GUI what it wanted */
    *wbTemp = nearestTemp;
    *wbTint = nearestTint;
}

/* Vignette Mask Creation */
void processingSetVignetteMask(processingObject_t *processing, uint16_t width, uint16_t height, float radius, float shape, float xStretch, float yStretch)
{
    double wHalf = width / 2.0;
    double hHalf = height / 2.0;
    double wHalfS = wHalf * xStretch;
    double hHalfS = hHalf * yStretch * (1.0 + shape * 2.0);
    double diagonal = sqrt( (wHalfS*wHalfS) + (hHalfS*hHalfS) );
    double r = diagonal * radius * ( 1.0 + shape / 5.0 );
    double T = diagonal - r;
    double cosTerm = 2.0*M_PI/T/4.0;

    processing->vignette_end = processing->vignette_mask + (width*height);

    //#pragma omp parallel for collapse(2)
    for( uint16_t x = 0; x < (uint16_t)wHalf; x++ )
    {
        #pragma omp parallel for
        for( uint16_t y = 0; y < (uint16_t)hHalf; y++ )
        {
            double w = fabs( (double)x * xStretch - wHalfS );
            double h = fabs( (double)y * yStretch - hHalfS );
            double d = sqrt( (w*w) + (h*h) );
            float val = 0.0;
            //only behind radius
            if( d > r )
            {
                //cos curve
                val = 0.5-0.5*cos(cosTerm*(d-r));
            }
            //4x faster...
            processing->vignette_mask[y*width+x] = val;
            processing->vignette_mask[(height-1-y)*width+x] = val;
            processing->vignette_mask[y*width+(width-1-x)] = val;
            processing->vignette_mask[(height-1-y)*width+(width-1-x)] = val;
        }
    }
}

void processingSetVignetteStrength(processingObject_t *processing, int8_t value)
{
    processing->vignette_strength = value;
}

/* Set and calculate the gradient alpha mask */
void processingSetGradientMask(processingObject_t *processing, uint16_t width, uint16_t height, float x1, float y1, float x2, float y2)
{
    float A = (x2 - x1);
    float B = (y2 - y1);
    float C1 = A * x1 + B * y1;
    float C2 = A * x2 + B * y2;

    for( uint16_t x = 0; x < width; x++ )
    {
        #pragma omp parallel for
        for( uint16_t y = 0; y < height; y++ )
        {
            float C = A * x + B * y;
            if( C <= C1 ) processing->gradient_mask[y*width+x] = 0;
            else if( C >= C2 ) processing->gradient_mask[y*width+x] = 65535;
            else
            {
                processing->gradient_mask[y*width+x] = LIMIT16( ( 65535 * ( C - C1 ) ) / ( C2 - C1 ) );
            }
        }
    }
}

/* Analyse dual iso frame to find highest green for highlight reconstruction */
void analyse_frame_highest_green(processingObject_t *processing, int imageX, int imageY, uint16_t *inputImage)
{
    //if not dual iso, we don't need to do this
    if ( *processing->dual_iso == 0 ) return;

    uint16_t * img = inputImage;
    int img_s = imageX * imageY * 3;
    uint16_t * img_end = img + img_s;
    int32_t ** pm = processing->pre_calc_matrix;
    int32_t ** pmg = processing->pre_calc_matrix_gradient;

    //printf( "start algo. \r\n" );

    //if ( processing->highlight_reconstruction )
    {
        /* for dual iso the highest green peak has to be searched */
        /* build histogram for green channel */
        uint16_t tableG[256] = {0};
        for (uint16_t * pix = img; pix < img_end; pix += 3)
        {
            uint16_t pix1 = LIMIT16( pm[4][processing->pre_calc_levels[pix[1]]] )>>8;
            if( pix1 > 255 ) pix1 = 255;
            tableG[pix1]++;
        }
        /* search the brightest (the most right) peak (I made it equivalent to the number of lines to process in the image or more) */
        int prevVal = 0;
        uint8_t dir = 0;
        uint8_t cnt = 0;
        int lastPeak = 1;
        //PARAMETERS
        int abrtDelt = 5000;
        int abrtPeak = imageX * imageY / 400;
        int abrtSign = imageX * imageY / 4000;
        // /////////
        for( int32_t i = 255; i > 2; i-- ) //only down to 5, below we just have dark noise, but no highlight
        {
            int curVal = tableG[i];
            if (prevVal < curVal) {  // (still) ascending?
                dir = 0;
            }
            else if (prevVal > curVal) { // (still) descending?
                if (dir != 1) { // starts descending?
                    int delta = prevVal-lastPeak;
                    //printf( "peak at index %d %d %d %d %d \r\n", (i-1)<<8, prevVal, cnt, abrtPeak, delta );
                    //This should be the highlight
                    if( prevVal > abrtPeak && delta > abrtDelt )
                    {
                        processing->highest_green_diso = (i-1)<<8;
                        break;
                    }
                    lastPeak = prevVal;
                    dir = 1;
                    //This should be already normal picture data... stop, there is no clipped highlight
                    if( prevVal > abrtSign )
                    {
                        processing->highest_green_diso = 65535;
                        break;
                    }
                    cnt++;
                }
            }
            // prevVal == curVal is simply ignored...
            prevVal = curVal;
        }

        /* And now the same for the gradient part image */
        if( processing->gradient_enable && ( ( processing->gradient_exposure_stops < -0.01 || processing->gradient_exposure_stops > 0.01 )
                                          || ( processing->gradient_contrast < -0.01 || processing->gradient_contrast > 0.01 ) ) )
        {
            uint16_t tableGg[256] = {0};
            for (uint16_t * pix = img; pix < img_end; pix += 3)
            {
                uint16_t pix1 = LIMIT16( pmg[4][processing->pre_calc_levels[pix[1]]] )>>8;
                if( pix1 > 255 ) pix1 = 255;
                tableGg[pix1]++;
            }
            /* search the brightest (the most right) peak (I made it equivalent to the number of lines to process in the image or more) */
            prevVal = 0;
            dir = 0;
            cnt = 0;
            for( int32_t i = 255; i > 2; i-- )
            {
                int curVal = tableGg[i];
                if (prevVal < curVal) {  // (still) ascending?
                    dir = 0;
                }
                else if (prevVal > curVal) { // (still) descending?
                    if (dir != 1) { // starts descending?
                        int delta = prevVal-lastPeak;
                        //This should be the highlight
                        if( prevVal > abrtPeak && delta > abrtDelt )
                        {
                            processing->highest_green_gradient_diso = (i-1)<<8;
                            break;
                        }
                        lastPeak = prevVal;
                        dir = 1;
                        //This should be already normal picture data... stop, there is no clipped highlight
                        if( prevVal > abrtSign )
                        {
                            processing->highest_green_gradient_diso = 65535;
                            break;
                        }
                        cnt++;
                    }
                }
                // prevVal == curVal is simply ignored...
                prevVal = curVal;
            }
        }
    }
}

//Set LUT strength factor
void processingSetLutStrength(processingObject_t *processing, uint8_t strength)
{
    processing->lut->intensity = strength;
}

//Set the gradation curve
void processingSetGCurve(processingObject_t *processing, int num, float *pXin, float *pYin, uint8_t channel)
{
    uint16_t *curve;
    if( channel == 1 ) curve = processing->gcurve_r;
    else if( channel == 2 ) curve = processing->gcurve_g;
    else if( channel == 3 ) curve = processing->gcurve_b;
    else curve = processing->gcurve_y;

    //Init
    if( num < 2 )
    {
        for( int i = 0; i < 65536; i++ )
        {
            curve[i] = i;
        }
        return;
    }

    //Build output sets
    float *pXout = (float*)malloc( sizeof(float) * 65536 );
    float *pYout = (float*)malloc( sizeof(float) * 65536 );

    int numOut = 65536;

    //Data into x of output sets
    for( int i = 0; i < numOut; i++ )
    {
        pXout[i] = i / (float)65536.0;
    }

    //Get the interpolated line
    int ret = spline1dc( pXin , pYin , &num,
                         pXout, pYout, &numOut );

    if( ret == 0 )
    {
        for( int i = 0; i < 65536; i++ )
        {
            if( pYout[i] > 1.0 ) pYout[i] = 1.0;
            else if( pYout[i] < 0.0001 ) pYout[i] = 0.0001;
            curve[i] = pYout[i] * 65535.0;
        }
    }

    free( pXout );
    free( pYout );
}

//Set the hue vs curves
void processingSetHueVsCurves(processingObject_t *processing, int num, float *pXin, float *pYin, uint8_t channel)
{
    float *curve;
    uint8_t *used;
    if( channel == 0 )
    {
        curve = processing->hue_vs_hue;
        used = &processing->hue_vs_hue_used;
    }
    else if( channel == 1 )
    {
        curve = processing->hue_vs_saturation;
        used = &processing->hue_vs_saturation_used;
    }
    else if( channel == 2 )
    {
        curve = processing->hue_vs_luma;
        used = &processing->hue_vs_luma_used;
    }
    else
    {
        curve = processing->luma_vs_saturation;
        used = &processing->luma_vs_saturation_used;
    }
    //Init
    if( num < 2 )
    {
        for( int i = 0; i < 36000; i++ )
        {
            curve[i] = 0.0;
            *used = 0;
        }
        return;
    }

    //Build output sets
    float *pXout = (float*)malloc( sizeof(float) * 36000 );
    float *pYout = (float*)malloc( sizeof(float) * 36000 );

    int numOut = 36000;

    //Data into x of output sets
    for( int i = 0; i < numOut; i++ )
    {
        pXout[i] = i / (float)36000.0;
    }

    //Get the interpolated line
    int ret = cosine_interpolate( pXin , pYin , &num,
                                  pXout, pYout, &numOut );

    if( ret == 0 )
    {
        *used = 0;
        for( int i = 0; i < 36000; i++ )
        {
            if( pYout[i] > 1.0 ) pYout[i] = 1.0;
            else if( pYout[i] < -1.0 ) pYout[i] = -1.0;
            curve[i] = pYout[i];
            if( curve[i] != 0.0 ) *used = 1;
        }
    }

    free( pXout );
    free( pYout );
}

/* Toning */
void processingSetToning(processingObject_t *processing, uint8_t r, uint8_t g, uint8_t b, uint8_t strength)
{
    processing->toning_dry = (100.0 - strength / 3.0) / 100.0;
    processing->toning_wet[0] = (strength / 3.0 / 100.0) * (float)r / 255.0;
    processing->toning_wet[1] = (strength / 3.0 / 100.0) * (float)g / 255.0;
    processing->toning_wet[2] = (strength / 3.0 / 100.0) * (float)b / 255.0;
}

static char * compile_ternary(char * function)
{
    /* Unforutnately tinyexpr doesnt have ternary operators (no library I could find did anyway) */
    int string_length = strlen(function);
    /* Count amount of quesiton marks and colons, to make sure theres no mroe than of one each */
    int question_mark_pos = -1, num_question_marks = 0;
    int colon_pos = -1, num_colons = 0;
    char comparison_operator = 0; /* This may be a '<' or a '>', if there is an '=' it is ignored */
    int comparison_operator_loc = -1, num_comparisons = 0;

    char * function_string = NULL;
    char * invalid_output = ((char*)(intptr_t)(1));

    for (int i = 0; i < string_length; ++i)
    {
        char current = function[i];
        if (current == '?')
        {
            num_question_marks++;
            question_mark_pos = i;
        }
        else if (current == ':')
        {
            num_colons++;
            colon_pos = i;
        }
        else if (current == '<' || current == '>')
        {
            comparison_operator = current;
            comparison_operator_loc = i;
            num_comparisons++;
        }
    }

    /* Must be equal for a valid ternary operation */
    if (num_question_marks != num_colons || num_comparisons != num_colons) return invalid_output;

    if (num_question_marks == 1)
    {
        int found_splitval = 0;
        double split_val = NAN;
        function_string = malloc(100 + string_length);

        char * splitval_str = function + comparison_operator_loc;

        while (*splitval_str != '?' && *splitval_str != 0)
        {
            if (isdigit(*splitval_str))
            {
                found_splitval = 1;
                split_val = atof(splitval_str);
                break;
            }
            ++splitval_str;
        }

        if (!found_splitval) return invalid_output;

        char * ternary_left = malloc(string_length + 10);
        strcpy(ternary_left, function + question_mark_pos + 1);
        for (int i = 0; ternary_left[i]; ++i) if (ternary_left[i] == ':') ternary_left[i] = 0;

        if (comparison_operator == '<')
            sprintf(function_string, "split(x, %lf, %s, %s)", split_val, ternary_left, function + colon_pos + 1);
        else if (comparison_operator == '>')
            sprintf(function_string, "split(x, %lf, %s, %s)", split_val, function + colon_pos + 1, ternary_left);
        else {
            free(ternary_left);
            return invalid_output;
        }
        free(ternary_left);
    }
    else if (num_question_marks == 0)
    {
        function_string = malloc(string_length+1);
        strcpy(function_string, function);
    }

    return function_string;
}

void processingSetGammaAndTonemapping(processingObject_t * processing, double gamma, int tonemapping)
{
    if (gamma < 1.00001 && gamma > 0.99999)
    {
        processingSetTransferFunction(processing, get_tonemap_func_string(tonemapping));
    }
    else
    {
        char * compiled = compile_ternary(get_tonemap_func_string(tonemapping));
        char * string = malloc(strlen(compiled)+100);
        sprintf(string, "pow(%s, %lf)", compiled, gamma);
        processingSetTransferFunction(processing, string);
        free(string);
        free(compiled);
    }
}

/* Transfer funciton, the correct version of "Gamma" or "Log" */
int processingSetTransferFunction(processingObject_t * processing, char * function)
{
#if defined(__linux) || defined(__APPLE__)
    setlocale(LC_NUMERIC, "C");
#endif
    char * function_string = compile_ternary(function);
    if (function_string == NULL) return 1;

    te_variable * var = &processing->x_variable;
    te_expr * expression = te_compile(function_string, var, 1, NULL);
    if (expression == NULL) {free(function_string); return 1;}

    if (processing->transfer_function_string != NULL) free(processing->transfer_function_string);
    if (processing->transfer_function_string_formatted != NULL) free(processing->transfer_function_string_formatted);
    te_free(processing->transfer_function);

    processing->transfer_function_string = malloc(strlen(function)+1);
    strcpy(processing->transfer_function_string, function);
    processing->transfer_function_string_formatted = function_string;
    processing->transfer_function = expression;

    /* This just updates the lookup tables now */
    processingSetGamma(processing, 1);

    return 0;
}

char * processingGetTransferFunction(processingObject_t * processing)
{
    return processing->transfer_function_string;
}

/* Luma weights for a colour gamut, mirroring the per-gamut derivation used in
 * the main processing loop (rgb_to_Y = second row of inverse(gamut RGB->XYZ)).
 * processing.c (which defines the file-local colour_gamuts table) is unity-
 * #included into this TU, so the table and invertMatrix are both in scope here. */
void processingGamutRgbToY(int colour_gamut, double out_rgb_to_Y[3])
{
    double inversemat[9];
    invertMatrix(colour_gamuts[colour_gamut], inversemat);
    for (int i = 0; i < 3; ++i) out_rgb_to_Y[i] = inversemat[3 + i];
}

/* AgX forward (compressed-gamut) matrix and its inverse, mirroring how the main
 * loop sources them (forward = the file-scope agx_compressed_matrix at ~613;
 * inverse = invertMatrix(agx_compressed_matrix), as at ~2686). Both are in scope
 * in this unity TU; exposed so the GPU-preview config can carry float-narrowed
 * copies without referencing the engine global from C++. */
void processingAgxMatrices(double out_forward[9], double out_inverse[9])
{
    for (int i = 0; i < 9; ++i) out_forward[i] = agx_compressed_matrix[i];
    invertMatrix(agx_compressed_matrix, out_inverse);
}
