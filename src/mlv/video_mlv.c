#include <stdint.h>
#include <limits.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <pthread.h>
#include <math.h>
#include <time.h>
#include <inttypes.h>
#include "camid/camera_id.h"
#include "macros.h"

#include <unistd.h>
#if defined(__linux)
#include <alloca.h>
#endif

#include "video_mlv.h"
#include "../debug/StageTiming.h"
#include "pipeline_stage_capture.h"
#include "audio_mlv.h"

#include "raw.h"
#include "mlv.h"
#include "llrawproc/llrawproc.h"
#include "mcraw/mcraw.h"

/* Debayering module */
#include "../debayer/debayer.h"
/* Processing module */
#include "../processing/raw_processing.h"
/* Phase 4B: fused downsample-and-debayer for adaptive playback resolution. */
#include "../processing/playback_downsample.h"

/* Lossless decompression */
#include "liblj92/lj92.h"

/* JPEG2000 decompression via OpenJPH */
#ifdef ENABLE_JPEG2K
#include "OpenJPH/ojph_wrapper.h"
#endif

/* Bitunpack and lossless compression */
#include "../dng/dng.h"

/* CinemaDNG folder source (reads per-frame .dng files for playback) */
#include "../dng/dng_reader.h"

#define MIN(a,b) (((a)<(b))?(a):(b))
#define MAX(a,b) (((a)>(b))?(a):(b))
#define ROR32(v,a) ((v) >> (a) | (v) << (32-(a)))

static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_disk_read_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_decompress_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_decompress_prepare_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_decompress_execute_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred6_split_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred6_split_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_generic_split_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_generic_split_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred1_fast_path_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_scan_component_count = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_write_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_expected_write_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_skip_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_linearize_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_component_count = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_lj92_predictor = -1;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred6_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred6_predictor_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_generic_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_generic_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_generic_predictor_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_unpack_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_copy_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_raw_uint16_prefetch_hit = 0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_llrawproc_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_float_convert_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_debayered_frame_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processing_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed16_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed16_for_8bit_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed16_to_8bit_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed16_cache_store_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed8_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_mlv_last_processed8_cache_store_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_processed8_direct_path_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_processed8_cache_hit = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_processed8_cache_hit_scale_factor = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_last_processed8_prefetch_hit = 0;

static uint64_t file_set_pos(FILE *stream, uint64_t offset, int whence)
{
#if defined(__WIN32)
    return fseeko64(stream, offset, whence);
#else
    return fseek(stream, offset, whence);
#endif
}

static uint64_t file_get_pos(FILE *stream)
{
#if defined(__WIN32)
    return ftello64(stream);
#else
    return ftell(stream);
#endif
}

#ifndef STDOUT_SILENT
#define DEBUG(CODE) CODE
#else
#define DEBUG(CODE)
#endif

#ifdef __WIN32
#define FMT_SIZE "%u"
#else
#define FMT_SIZE "%zu"
#endif

#define MLV_FNV1A_OFFSET_BASIS UINT64_C(14695981039346656037)
#define MLV_FNV1A_PRIME UINT64_C(1099511628211)
#define MLV_RAW_UINT16_PREFETCH_EMPTY 0
#define MLV_RAW_UINT16_PREFETCH_READY 1
#define MLV_RAW_UINT16_PREFETCH_DECODING 2
#define MLV_RAW_UINT16_PREFETCH_LOOKAHEAD 2
#define MLV_RAW_UINT16_PREFETCH_MAX_PREDICTIVE_STRIDE 32
#define MLV_PROCESSED_8BIT_PREFETCH_EMPTY 0
#define MLV_PROCESSED_8BIT_PREFETCH_READY 1
#define MLV_PROCESSED_8BIT_PREFETCH_RENDERING 2
#define MLV_PROCESSED_8BIT_PREFETCH_LOOKAHEAD 2 /* default; runtime-tunable via env MLVAPP_PROCESSED8_LOOKAHEAD (clamped 1..MLV_PROCESSED_8BIT_CACHE_SLOTS-1) for A/B without rebuilds - see mlv_processed8_prefetch_lookahead_value(). */

#if defined(_MSC_VER)
#define MLV_THREAD_LOCAL __declspec(thread)
#else
#define MLV_THREAD_LOCAL __thread
#endif

static void mlv_reset_last_raw_stage_telemetry(void)
{
    g_mlv_last_raw_uint16_ms = 0.0;
    g_mlv_last_raw_uint16_disk_read_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_prepare_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_execute_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_split_active = 0;
    g_mlv_last_raw_uint16_lj92_pred6_split_requested = 0;
    g_mlv_last_raw_uint16_lj92_generic_split_active = 0;
    g_mlv_last_raw_uint16_lj92_generic_split_requested = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_active = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible = 0;
    g_mlv_last_raw_uint16_lj92_scan_component_count = 0;
    g_mlv_last_raw_uint16_lj92_write_length = 0;
    g_mlv_last_raw_uint16_lj92_expected_write_length = 0;
    g_mlv_last_raw_uint16_lj92_skip_length = 0;
    g_mlv_last_raw_uint16_lj92_linearize_active = 0;
    g_mlv_last_raw_uint16_lj92_component_count = 0;
    g_mlv_last_raw_uint16_lj92_predictor = -1;
    g_mlv_last_raw_uint16_lj92_pred6_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_unpack_ms = 0.0;
    g_mlv_last_raw_uint16_copy_ms = 0.0;
    g_mlv_last_raw_uint16_prefetch_hit = 0;
}

static int mlv_env_value_is_truthy(const char * value)
{
    if (!value || value[0] == '\0') return 0;
    if (strcmp(value, "1") == 0) return 1;
    if (strcasecmp(value, "true") == 0) return 1;
    if (strcasecmp(value, "yes") == 0) return 1;
    if (strcasecmp(value, "on") == 0) return 1;
    return 0;
}

static int g_mlv_playback_aggressive_preview_mode = 0;
static int g_mlv_playback_fast_x4_hq_path_mode = 0;

void mlvSetPlaybackAggressivePreviewMode(int enabled)
{
#if defined(__GNUC__)
    __atomic_store_n(&g_mlv_playback_aggressive_preview_mode,
                     enabled ? 1 : 0,
                     __ATOMIC_RELEASE);
#else
    g_mlv_playback_aggressive_preview_mode = enabled ? 1 : 0;
#endif
}

/* Round-3 item 1: GUI-selected playback preview resolution (proxy level).
 * -1 = Auto (each scale keeps its tuned default proxy), 0 = Full (no proxy,
 * sharpest/slowest), 1 = Half (one internal halving per axis vs the output -
 * the x1 half-res and x2 quarter-res preview cores), 2 = Quarter (two
 * halvings; falls back to Half until a quarter core exists for the scale).
 * The MLVAPP_DISABLE_* env kill switches still take precedence so harness
 * A/Bs and tests keep their exact contracts. */
static int g_mlv_playback_proxy_level = -1;

void mlvSetPlaybackProxyLevel(int level)
{
    int normalized = level;
    if (normalized < -1) normalized = -1;
    if (normalized > 2) normalized = 2;
#if defined(__GNUC__)
    __atomic_store_n(&g_mlv_playback_proxy_level, normalized, __ATOMIC_RELEASE);
#else
    g_mlv_playback_proxy_level = normalized;
#endif
}

int mlvPlaybackProxyLevel(void)
{
#if defined(__GNUC__)
    return __atomic_load_n(&g_mlv_playback_proxy_level, __ATOMIC_ACQUIRE);
#else
    return g_mlv_playback_proxy_level;
#endif
}

static int mlv_playback_aggressive_preview_env_override(void)
{
    const char * value = getenv("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW");
    if (value && value[0] != '\0')
    {
        if (strcmp(value, "0") == 0
         || strcasecmp(value, "false") == 0
         || strcasecmp(value, "off") == 0
         || strcasecmp(value, "sharp") == 0
         || strcasecmp(value, "sharp_smooth") == 0
         || strcasecmp(value, "smooth") == 0
         || strcasecmp(value, "quality") == 0)
        {
            return 0;
        }
        if (mlv_env_value_is_truthy(value)
         || strcasecmp(value, "aggressive") == 0
         || strcasecmp(value, "aggressive_performance") == 0
         || strcasecmp(value, "performance") == 0
         || strcasecmp(value, "fast") == 0)
        {
            return 1;
        }
        return 0;
    }

    value = getenv("MLVAPP_PLAYBACK_PREVIEW_MODE");
    if (!value || value[0] == '\0')
    {
        return -1;
    }
    if (strcmp(value, "0") == 0
     || strcasecmp(value, "sharp") == 0
     || strcasecmp(value, "sharp_smooth") == 0
     || strcasecmp(value, "smooth") == 0
     || strcasecmp(value, "quality") == 0)
    {
        return 0;
    }
    if (mlv_env_value_is_truthy(value)
     || strcasecmp(value, "aggressive") == 0
     || strcasecmp(value, "aggressive_performance") == 0
     || strcasecmp(value, "performance") == 0
     || strcasecmp(value, "fast") == 0)
    {
        return 1;
    }
    return 0;
}

int mlvPlaybackAggressivePreviewMode(void)
{
    const int envOverride = mlv_playback_aggressive_preview_env_override();
    if (envOverride >= 0)
    {
        return envOverride;
    }
#if defined(__GNUC__)
    return __atomic_load_n(&g_mlv_playback_aggressive_preview_mode,
                           __ATOMIC_ACQUIRE) != 0;
#else
    return g_mlv_playback_aggressive_preview_mode != 0;
#endif
}

void mlvSetPlaybackFastX4HqPathMode(int enabled)
{
#if defined(__GNUC__)
    __atomic_store_n(&g_mlv_playback_fast_x4_hq_path_mode,
                     enabled ? 1 : 0,
                     __ATOMIC_RELEASE);
#else
    g_mlv_playback_fast_x4_hq_path_mode = enabled ? 1 : 0;
#endif
}

int mlvPlaybackFastX4HqPathMode(void)
{
#if defined(__GNUC__)
    return __atomic_load_n(&g_mlv_playback_fast_x4_hq_path_mode,
                           __ATOMIC_ACQUIRE) != 0;
#else
    return g_mlv_playback_fast_x4_hq_path_mode != 0;
#endif
}

/* Matches lj92.c's lj92_pred6_split_enabled-style truthy check: any non-empty
 * value other than literal "0" enables the feature. Broader than
 * mlv_env_value_is_truthy so the prefetch disable here tracks whatever lj92
 * considers "profiling requested". */
static int mlv_env_value_is_lj92_truthy(const char * value)
{
    if (!value || value[0] == '\0') return 0;
    if (value[0] == '0' && value[1] == '\0') return 0;
    return 1;
}

static int mlv_raw_uint16_prefetch_enabled(void)
{
    static int enabled = -1;
    if (enabled >= 0)
    {
        return enabled;
    }

    if (mlv_env_value_is_truthy(getenv("MLVAPP_DISABLE_RAW_UINT16_PREFETCH")))
    {
        enabled = 0;
    }
    else if (mlv_env_value_is_lj92_truthy(getenv("MLVAPP_PROFILE_LJ92_PRED6_SPLIT"))
             || mlv_env_value_is_lj92_truthy(getenv("MLVAPP_PROFILE_LJ92_GENERIC_SPLIT"))
             || mlv_env_value_is_lj92_truthy(getenv("MLVAPP_PRED1_FASTPATH_MEASUREMENT")))
    {
        /* Invariant (future maintainers, preserve this):
         *   These profiling modes populate THREAD-LOCAL telemetry on the
         *   decoding thread. The consumer then reads those TLs back on its
         *   own thread after the decode returns. For the signals to reach
         *   the consumer at all, DECODE MUST REMAIN ON THE CALLER / READ
         *   THREAD — running it in the prefetch worker puts the TLs on the
         *   worker thread, where the consumer never sees them.
         *
         * Therefore: while any of these profiling env vars are truthy, the
         * prefetch is implicitly disabled so decode stays in-thread. This
         * gate is the single place that enforces the invariant; do not try
         * to propagate thread-locality awareness through the worker path. */
        enabled = 0;
    }
    else
    {
        enabled = 1;
    }
    return enabled;
}

static int mlv_phase4bv2_disabled_via_env(void);
static int mlv_phase4bv3_disabled_via_env(void);
static int mlv_phase4bv4_x8_disabled_via_env(void);
static int mlv_phase4bv4_x8_preview_compatible(const mlvObject_t * video);

static int mlv_aggressive_reduced_dual_iso_prefetch_expected(const mlvObject_t * video)
{
    if (!mlvPlaybackAggressivePreviewMode() || !video || !video->llrawproc)
    {
        return 0;
    }
    if (mlv_phase4bv2_disabled_via_env())
    {
        return 0;
    }

    const llrawprocObject_t * shared = video->llrawproc;
    if (shared->dual_iso != 1 || !shared->diso_validity || !shared->fix_raw)
    {
        return 0;
    }
    if (video->playback_scale_factor_active == 8)
    {
        return !mlv_phase4bv4_x8_disabled_via_env()
            && mlv_phase4bv4_x8_preview_compatible(video);
    }
    if (video->playback_scale_factor_active == 4)
    {
        if (mlv_phase4bv3_disabled_via_env())
        {
            return 0;
        }
        const int full_w = (int)getMlvWidth(video);
        const int full_h = (int)getMlvHeight(video);
        const int eff_h = (full_h / 16) * 16;
        return full_w > 0 && full_h > 0 && (full_w % 4) == 0 && eff_h >= 16;
    }

    return 0;
}

static int mlv_dual_iso_scale4_prefetch_expected(const mlvObject_t * video)
{
    if (!video || !video->llrawproc)
    {
        return 0;
    }
    if (mlv_phase4bv2_disabled_via_env() || mlv_phase4bv3_disabled_via_env())
    {
        return 0;
    }

    const llrawprocObject_t * shared = video->llrawproc;
    if (shared->dual_iso != 1 || !shared->diso_validity || !shared->fix_raw)
    {
        return 0;
    }
    if (video->playback_scale_factor_active != 4)
    {
        return 0;
    }

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 16) * 16;
    return full_w > 0 && full_h > 0 && (full_w % 4) == 0 && eff_h >= 16;
}

static int mlv_dual_iso_scale8_prefetch_expected(const mlvObject_t * video)
{
    if (!video || !video->llrawproc)
    {
        return 0;
    }
    if (mlv_phase4bv2_disabled_via_env() || mlv_phase4bv4_x8_disabled_via_env())
    {
        return 0;
    }

    const llrawprocObject_t * shared = video->llrawproc;
    if (shared->dual_iso != 1 || !shared->diso_validity || !shared->fix_raw)
    {
        return 0;
    }
    if (video->playback_scale_factor_active != 8)
    {
        return 0;
    }

    return mlv_phase4bv4_x8_preview_compatible(video);
}

static int mlv_raw_uint16_prefetch_allowed_for_request(const mlvObject_t * video)
{
    if (!mlv_raw_uint16_prefetch_enabled())
    {
        return 0;
    }

    if (!video || !video->llrawproc)
    {
        return 1;
    }

    if (video->playback_scale_factor_active >= 4
        && mlvPlaybackAggressivePreviewMode()
        && !mlv_aggressive_reduced_dual_iso_prefetch_expected(video))
    {
        return 0;
    }

    /* On full-recon HQ Dual ISO scale-4+ playback, the prefetch worker
     * competes with full20 reconstruction; foreground decode is cheaper there.
     * In aggressive reduced previews, Phase 4B has already moved recon/debayer
     * to preview-sized buffers, so decode overlap attacks the measured full-raw
     * decode floor without changing sharp/smooth fallback behavior. */
    if (video->llrawproc->dual_iso == 1
        && video->playback_scale_factor_active >= 4)
    {
        if (video->playback_scale_factor_active == 4
            && mlv_dual_iso_scale4_prefetch_expected(video))
        {
            return 1;
        }
        if (video->playback_scale_factor_active == 8
            && mlv_dual_iso_scale8_prefetch_expected(video))
        {
            return 1;
        }
        if (mlv_aggressive_reduced_dual_iso_prefetch_expected(video))
        {
            return 1;
        }
        return 0;
    }

    return 1;
}

static uint32_t mlv_raw_uint16_prefetch_lookahead_for_request(const mlvObject_t * video)
{
    if (video && (processingPlaybackPreviewModeEnabled() || mlvPlaybackAggressivePreviewMode()))
    {
        if (video->playback_scale_factor_active == 1)
        {
            return 4;
        }
        if (video->playback_scale_factor_active == 2)
        {
            return 10;
        }
        if (video->playback_scale_factor_active == 4)
        {
            return 8;
        }
        if (video->playback_scale_factor_active >= 8)
        {
            return 8;
        }
    }

    return MLV_RAW_UINT16_PREFETCH_LOOKAHEAD;
}

uint32_t mlvRawUint16PrefetchLookaheadForTesting(const mlvObject_t * video)
{
    return mlv_raw_uint16_prefetch_lookahead_for_request(video);
}

static int mlv_processed8_prefetch_enabled(const mlvObject_t * video)
{
    /* Keep the env override for experimentation, but let the x2/x4 aggressive
     * paths benefit by default when the runtime is already in that preview
     * mode. The prefetch worker does not change pixels; it only tries to
     * hide the shared processed8 render cost behind the next frame. */
    const char * value = getenv("MLVAPP_EXPERIMENTAL_PROCESSED8_PREFETCH");
    if (value && value[0] != '\0')
    {
        return (strcmp(value, "0") != 0
             && strcasecmp(value, "false") != 0
             && strcasecmp(value, "off") != 0)
            ? 1
            : 0;
    }

    if (video && mlvPlaybackAggressivePreviewMode())
    {
        return video->playback_scale_factor_active >= 1;
    }

    /* Standard sharp/smooth playback. x2/x4/x8 land hundreds of processed8
     * prefetch hits, so overlapping the next processed8 result there lets the
     * foreground spend more time presenting frames. x1 (full resolution) is
     * different: producing a full-res processed8 frame costs about as much as
     * the foreground frame itself, so the worker never gets ahead. Across the
     * standard M16 trio the x1 lane measured processed8_prefetch_hits=0 on
     * every clip while the worker still ran a full background RGB/dual-ISO/
     * shadows-highlights pipeline, stealing cores and disk reads from the
     * slowest priority lane for no benefit. Keep the prefetch off at x1. */
    return video
        && processingPlaybackPreviewModeEnabled()
        && (video->playback_scale_factor_active == 2
            || video->playback_scale_factor_active >= 4);
}

/* MLVAPP_PREFETCH_DEBUG=1: stderr signature/decision tracing for the
 * processed8 prefetch worker and the foreground lookup. Diagnostic only. */
static int mlv_processed8_prefetch_debug_enabled(void)
{
    static int cached = -1;
    if (cached < 0)
    {
        const char * value = getenv("MLVAPP_PREFETCH_DEBUG");
        cached = (value && value[0] != '\0' && strcmp(value, "0") != 0) ? 1 : 0;
    }
    return cached;
}

/* Worker-side fallback for direct8-incompatible processing states: render the
 * prefetch frame through the same indirect processed16->8 composition the
 * foreground uses. Default ON; MLVAPP_PROCESSED8_PREFETCH_INDIRECT=0/false/off
 * restores the pre-2026-06-11 behavior (worker skips incompatible states). */
static int mlv_processed8_prefetch_indirect_enabled(void)
{
    const char * value = getenv("MLVAPP_PROCESSED8_PREFETCH_INDIRECT");
    if (value && value[0] != '\0')
    {
        return (strcmp(value, "0") != 0
             && strcasecmp(value, "false") != 0
             && strcasecmp(value, "off") != 0)
            ? 1
            : 0;
    }
    return 1;
}

/* Round-2 item 2: the indirect worker render at x2 Sharp, re-A/B'd after the
 * quarter-res preview cheapened the worker's render. VERDICT (2026-06-12
 * interleaved A/B, M16 trio): DEAD END - the quarter-res foreground render
 * (~33 ms) already fits the 42 ms frame budget, so warm runs reach native
 * WITHOUT the worker, and with it the original wash-to-negative core-split
 * reappears on warm runs (1347: on 13.0 vs off 22.2 median fps) even though
 * worker hits are byte-faithful and can serve 100% of requests. Default OFF;
 * MLVAPP_PREFETCH_INDIRECT_X2=1 opts in for future re-audits (the mechanism
 * stays test-pinned). Read fresh each call so tests can flip it. */
static int mlv_processed8_prefetch_indirect_x2_enabled(void)
{
    const char * value = getenv("MLVAPP_PREFETCH_INDIRECT_X2");
    if (value && value[0] != '\0')
    {
        return (strcmp(value, "0") != 0
             && strcasecmp(value, "false") != 0
             && strcasecmp(value, "off") != 0)
            ? 1
            : 0;
    }
    return 0;
}

/* The prefetch worker renders with a PARTIAL copy of the foreground
 * processing state (mlv_copy_processed8_prefetch_processing_state): values,
 * curves, precalc tables, and matrices are carried, but LUT data, filter
 * internals, CS-zone data, gradient parameters, toning, hue-vs curves,
 * denoiser state, and grain are NOT. The full 16-bit pipeline consumes those,
 * so the indirect worker render is allowed only for states the copy provably
 * represents; anything else keeps the old skip and the foreground renders
 * honestly. A wrong-content cache hit is worse than a miss - fail closed. */
static int mlv_processed8_prefetch_indirect_state_supported(const processingObject_t * processing)
{
    if (!processing) return 0;
    /* The indirect worker carries the clarity scalar but not clarity_curve,
     * which applyProcessingObject consumes whenever clarity is non-neutral.
     * A wrong-content cache hit is worse than a miss: keep that state on the
     * foreground path until the complete derived table is cloned and pinned
     * by hit-vs-fresh parity coverage. */
    if (fabs(processing->clarity) >= 0.01) return 0;
    if (processing->lut_on) return 0;
    if (processing->filter_on) return 0;
    if (processing->gradient_enable) return 0;
    if (processing->vignette_strength != 0) return 0;
    if (processing->denoiserStrength > 0) return 0;
    if (processing->rbfDenoiserLuma > 0) return 0;
    if (processing->rbfDenoiserChroma > 0) return 0;
    if (processing->grainStrength > 0) return 0;
    if (processing->ca_desaturate > 0) return 0;
    if (processingUsesChromaSeparation((processingObject_t *)processing)) return 0;
    if (processingGetSharpening((processingObject_t *)processing) > 0.005) return 0;
    if (processing->allow_creative_adjustments
        && (processing->hue_vs_luma_used
            || processing->hue_vs_saturation_used
            || processing->hue_vs_hue_used
            || processing->luma_vs_saturation_used)) return 0;
    if (processing->toning_dry < 0.998f) return 0;
    if (fabsf(processing->toning_wet[0]) > 0.0005f
     || fabsf(processing->toning_wet[1]) > 0.0005f
     || fabsf(processing->toning_wet[2]) > 0.0005f) return 0;
    return 1;
}

static uint64_t mlv_hash_bytes(uint64_t hash, const void * data, size_t size)
{
    const uint8_t * bytes = (const uint8_t *)data;
    if (!bytes || !size)
    {
        return hash;
    }

    for (size_t i = 0; i < size; ++i)
    {
        hash ^= bytes[i];
        hash *= MLV_FNV1A_PRIME;
    }
    return hash;
}

static uint64_t mlv_hash_c_string(uint64_t hash, const char * value)
{
    if (!value)
    {
        return mlv_hash_bytes(hash, "", 1);
    }

    return mlv_hash_bytes(hash, value, strlen(value) + 1);
}

static uint64_t mlv_hash_sampled_bytes(uint64_t hash, const void * data, size_t size)
{
    const uint8_t * bytes = (const uint8_t *)data;
    const size_t sample_count = 64;
    if (!bytes || !size)
    {
        return hash;
    }

    hash = mlv_hash_bytes(hash, &size, sizeof(size));
    if (size <= sample_count)
    {
        return mlv_hash_bytes(hash, data, size);
    }

    for (size_t sample = 0; sample < sample_count; ++sample)
    {
        size_t offset = (sample * (size - 1)) / (sample_count - 1);
        hash = mlv_hash_bytes(hash, bytes + offset, 1);
    }

    return hash;
}

static uint64_t mlv_hash_pixel_map(uint64_t hash, const pixel_map * map)
{
    if (!map)
    {
        return mlv_hash_bytes(hash, "", 1);
    }

    hash = mlv_hash_bytes(hash, &map->type, sizeof(map->type));
    hash = mlv_hash_bytes(hash, &map->count, sizeof(map->count));
    hash = mlv_hash_bytes(hash, &map->capacity, sizeof(map->capacity));
    if (map->pixels && map->count)
    {
        hash = mlv_hash_bytes(hash, map->pixels, map->count * sizeof(pixel_xy));
    }
    return hash;
}

static uint64_t mlv_hash_filter_object(uint64_t hash, const filterObject_t * filter)
{
    if (!filter)
    {
        return mlv_hash_bytes(hash, "", 1);
    }

    hash = mlv_hash_bytes(hash, &filter->strength, sizeof(filter->strength));
    hash = mlv_hash_bytes(hash, &filter->filter_option, sizeof(filter->filter_option));
    hash = mlv_hash_bytes(hash, filter->processed, sizeof(filter->processed));
    hash = mlv_hash_bytes(hash, filter->original, sizeof(filter->original));
    return hash;
}

static uint64_t mlv_hash_lut(uint64_t hash, const lut_t * lut)
{
    if (!lut)
    {
        return mlv_hash_bytes(hash, "", 1);
    }

    hash = mlv_hash_bytes(hash, lut->title, sizeof(lut->title));
    hash = mlv_hash_bytes(hash, &lut->dimension, sizeof(lut->dimension));
    hash = mlv_hash_bytes(hash, lut->domain_min, sizeof(lut->domain_min));
    hash = mlv_hash_bytes(hash, lut->domain_max, sizeof(lut->domain_max));
    hash = mlv_hash_bytes(hash, &lut->is3d, sizeof(lut->is3d));
    hash = mlv_hash_bytes(hash, &lut->intensity, sizeof(lut->intensity));

    if (lut->cube && lut->dimension)
    {
        uint64_t cube_entries = lut->is3d
            ? (uint64_t)lut->dimension * lut->dimension * lut->dimension * 3
            : (uint64_t)lut->dimension * 3;
        if (cube_entries <= (uint64_t)SIZE_MAX / sizeof(float))
        {
            hash = mlv_hash_sampled_bytes(hash, lut->cube, (size_t)cube_entries * sizeof(float));
        }
    }

    return hash;
}

static uint64_t mlv_hash_llrawproc_state(uint64_t hash, const llrawprocObject_t * llrawproc)
{
    if (!llrawproc)
    {
        return mlv_hash_bytes(hash, "", 1);
    }

    hash = mlv_hash_bytes(hash, &llrawproc->fix_raw, sizeof(llrawproc->fix_raw));
    hash = mlv_hash_bytes(hash, &llrawproc->vertical_stripes, sizeof(llrawproc->vertical_stripes));
    hash = mlv_hash_bytes(hash, &llrawproc->compute_stripes, sizeof(llrawproc->compute_stripes));
    hash = mlv_hash_bytes(hash, &llrawproc->focus_pixels, sizeof(llrawproc->focus_pixels));
    hash = mlv_hash_bytes(hash, &llrawproc->fpi_method, sizeof(llrawproc->fpi_method));
    hash = mlv_hash_bytes(hash, &llrawproc->fpm_status, sizeof(llrawproc->fpm_status));
    hash = mlv_hash_bytes(hash, &llrawproc->bad_pixels, sizeof(llrawproc->bad_pixels));
    hash = mlv_hash_bytes(hash, &llrawproc->bps_method, sizeof(llrawproc->bps_method));
    hash = mlv_hash_bytes(hash, &llrawproc->bpi_method, sizeof(llrawproc->bpi_method));
    hash = mlv_hash_bytes(hash, &llrawproc->bpm_status, sizeof(llrawproc->bpm_status));
    hash = mlv_hash_bytes(hash, &llrawproc->chroma_smooth, sizeof(llrawproc->chroma_smooth));
    hash = mlv_hash_bytes(hash, &llrawproc->pattern_noise, sizeof(llrawproc->pattern_noise));
    hash = mlv_hash_bytes(hash, &llrawproc->deflicker_target, sizeof(llrawproc->deflicker_target));
    hash = mlv_hash_bytes(hash, &llrawproc->diso_validity, sizeof(llrawproc->diso_validity));
    hash = mlv_hash_bytes(hash, &llrawproc->dual_iso, sizeof(llrawproc->dual_iso));
    hash = mlv_hash_bytes(hash, &llrawproc->diso1, sizeof(llrawproc->diso1));
    hash = mlv_hash_bytes(hash, &llrawproc->diso2, sizeof(llrawproc->diso2));
    hash = mlv_hash_bytes(hash, &llrawproc->diso_auto_correction, sizeof(llrawproc->diso_auto_correction));
    hash = mlv_hash_bytes(hash, &llrawproc->diso_averaging, sizeof(llrawproc->diso_averaging));
    /* Mean23 playback override is part of the steady-state cache key: the
     * same frame index produces different pixels with override on (mean23)
     * vs off (AMaZE), so the processed-frame cache must keep both alive
     * across a paused -> playing transition. See note at dualiso.c on the
     * playback policy that flips this field on/off. */
    hash = mlv_hash_bytes(hash, &llrawproc->diso_playback_force_mean23, sizeof(llrawproc->diso_playback_force_mean23));
    hash = mlv_hash_bytes(hash, &llrawproc->diso_alias_map, sizeof(llrawproc->diso_alias_map));
    hash = mlv_hash_bytes(hash, &llrawproc->diso_frblending, sizeof(llrawproc->diso_frblending));
    /* Phase E5 scale-aware downgrade overrides: same cache-key principle
     * as the mean23 override above. With override on (alias_map / FR
     * blending suppressed) the recon produces different pixels than with
     * override off, so the processed-frame cache must keep both alive
     * across a paused -> playing transition. */
    hash = mlv_hash_bytes(hash, &llrawproc->diso_playback_force_disable_alias_map, sizeof(llrawproc->diso_playback_force_disable_alias_map));
    hash = mlv_hash_bytes(hash, &llrawproc->dark_frame, sizeof(llrawproc->dark_frame));
    /* Phase 2C: diso_pattern, diso_ev_correction, diso_black_delta and the
     * dng_* fields are auto-published per frame by Dual ISO recon (see
     * src/mlv/llrawproc/llrawproc.c:llrawproc_publish_worker_results) and
     * therefore drift across consecutive frames in steady-state playback
     * (e.g. patt 0 -> -4 and dng_white_level 2840 -> 12688 between the
     * first and second frame on the large_dual_iso fixture).
     *
     * Including them in the cache state hash forces every frame's
     * note_request to bump the prefetch generation counter, which
     * silently invalidates the processed8 8-slot cache. The render thread
     * then re-runs the full pipeline every frame.
     *
     * User-driven changes to any of these fields (manual ev/black-delta
     * sliders, pattern combobox, etc.) flow through resetMlvCache() in
     * the GUI handlers (platform/qt/MainWindow.cpp:8482-8531, 11082) which
     * calls invalidateMlvProcessedPreviewCache() and zeros all 8 cache
     * slots. Cache invalidation on user intent is preserved without
     * relying on the hash. Mode (auto vs manual) stays in the hash via
     * diso_auto_correction (above).
     *
     * See .claude-state/profiling/20260424-phase2c-prefetch-fix/ for
     * diagnostic logs identifying the drift fields. */
    hash = mlv_hash_c_string(hash, llrawproc->dark_frame_filename);
    hash = mlv_hash_bytes(hash, &llrawproc->dark_frame_hdr, sizeof(llrawproc->dark_frame_hdr));
    hash = mlv_hash_bytes(hash, &llrawproc->dark_frame_size, sizeof(llrawproc->dark_frame_size));
    if (llrawproc->dark_frame_data && llrawproc->dark_frame_size)
    {
        hash = mlv_hash_bytes(hash, llrawproc->dark_frame_data, llrawproc->dark_frame_size);
    }
    hash = mlv_hash_pixel_map(hash, &llrawproc->focus_pixel_map);
    hash = mlv_hash_pixel_map(hash, &llrawproc->bad_pixel_map);
    return hash;
}

/* Phase 4A: validate the requested playback scale factor. Accepts 1, 2, 4,
 * or 8; everything else is clamped to 1. Centralised so cache-key, slot
 * match, and the dimensions helper all agree on what counts as valid. */
static int mlv_normalize_playback_scale_factor(int scaleFactor)
{
    if (scaleFactor == 2 || scaleFactor == 4 || scaleFactor == 8)
    {
        return scaleFactor;
    }
    return 1;
}

/* Phase 4B: resolve the effective scale factor for a given video object.
 * Explicit scale=2/8 must stay at the requested scale. Fast pre-recon Dual ISO
 * shortcuts are optional internal routes for compatible x4/x8 playback;
 * incompatible cases fall back to full-res recon then post-recon downsample so
 * the user still gets the requested reduced image. Exact block alignment
 * remains required for x2/x4. The explicit x8 preview path may crop trailing
 * partial 8x8 edge blocks, so common 4-aligned clips can still use the deeper
 * preview scale. */
static int mlv_effective_playback_scale_factor(mlvObject_t * video, int requestedScale)
{
    int s = mlv_normalize_playback_scale_factor(requestedScale);
    if (s == 1) return 1;

    if (!video) return 1;

    const int width = (int)getMlvWidth(video);
    const int height = (int)getMlvHeight(video);
    if (width <= 0 || height <= 0) return 1;

    if (s == 8)
    {
        return (width >= 8 && height >= 8) ? 8 : 1;
    }

    /* Block alignment: width and height must be a multiple of the selected
     * scale for the post-llrawproc 2x/4x kernels and the x4 pre-recon path. */
    if ((width % s) != 0 || (height % s) != 0)
    {
        return 1;
    }

    return s;
}

static uint64_t mlv_processed_frame_state_signature_with_scale(mlvObject_t * video,
                                                               int scaleFactor);
static int mlv_halfres_x1_preview_enabled(void);
static int mlv_halfres_x1_processing_enabled(void);
static int mlv_quarterres_x2_preview_enabled(void);
static int mlv_quarterres_x2_processing_enabled(void);
static int mlv_x1_reduced_proxy_would_engage(mlvObject_t * video);
static int mlv_preview_direct8_input_is_cheap(mlvObject_t * video, int normalizedScale);

static uint64_t mlv_processed_frame_state_signature(mlvObject_t * video)
{
    return mlv_processed_frame_state_signature_with_scale(video, 1);
}

static uint64_t mlv_processed_frame_state_signature_with_scale(mlvObject_t * video,
                                                               int scaleFactor)
{
    uint64_t hash = MLV_FNV1A_OFFSET_BASIS;
    processingObject_t * processing = video ? video->processing : NULL;

    /* Phase 4A: hash the normalised scale factor so a scale=1 entry never
     * collides with a scale=2 lookup once Phase 4B starts producing scaled
     * output. Today the pipeline ignores it, but the cache key already
     * distinguishes them. */
    const int normalizedScale = mlv_normalize_playback_scale_factor(scaleFactor);
    hash = mlv_hash_bytes(hash, &normalizedScale, sizeof(normalizedScale));
    const int aggressivePreview = mlvPlaybackAggressivePreviewMode();
    hash = mlv_hash_bytes(hash, &aggressivePreview, sizeof(aggressivePreview));
    const int fastX4HqPath = mlvPlaybackFastX4HqPathMode();
    hash = mlv_hash_bytes(hash, &fastX4HqPath, sizeof(fastX4HqPath));
    if (normalizedScale == 1)
    {
        const int previewMode = processingPlaybackPreviewModeEnabled();
        const int halfresX1Preview = mlv_halfres_x1_preview_enabled();
        const int halfresX1Processing = mlv_halfres_x1_processing_enabled();
        const int proxyLevel = mlvPlaybackProxyLevel();
        hash = mlv_hash_bytes(hash, &previewMode, sizeof(previewMode));
        hash = mlv_hash_bytes(hash, &halfresX1Preview, sizeof(halfresX1Preview));
        hash = mlv_hash_bytes(hash, &halfresX1Processing, sizeof(halfresX1Processing));
        hash = mlv_hash_bytes(hash, &proxyLevel, sizeof(proxyLevel));
    }
    if (normalizedScale == 2)
    {
        /* Round-3 item 1: the GUI proxy-level setting can flip the quarter-res
         * x2 preview at runtime (the env kill switch never could mid-process),
         * so the cache key must distinguish the two render paths. */
        const int previewMode = processingPlaybackPreviewModeEnabled();
        const int quarterresX2Preview = mlv_quarterres_x2_preview_enabled();
        const int quarterresX2Processing = mlv_quarterres_x2_processing_enabled();
        hash = mlv_hash_bytes(hash, &previewMode, sizeof(previewMode));
        hash = mlv_hash_bytes(hash, &quarterresX2Preview, sizeof(quarterresX2Preview));
        hash = mlv_hash_bytes(hash, &quarterresX2Processing, sizeof(quarterresX2Processing));
    }

    if (!video)
    {
        return hash;
    }

    hash = mlv_hash_bytes(hash, &video->use_amaze, sizeof(video->use_amaze));
    hash = mlv_hash_bytes(hash, &video->ca_red, sizeof(video->ca_red));
    hash = mlv_hash_bytes(hash, &video->ca_blue, sizeof(video->ca_blue));
    hash = mlv_hash_bytes(hash, &video->RAWI.raw_info.black_level, sizeof(video->RAWI.raw_info.black_level));
    hash = mlv_hash_bytes(hash, &video->RAWI.raw_info.white_level, sizeof(video->RAWI.raw_info.white_level));
    hash = mlv_hash_llrawproc_state(hash, video->llrawproc);

    if (!processing)
    {
        return hash;
    }

    hash = mlv_hash_bytes(hash, &processing->exr_mode, sizeof(processing->exr_mode));
    hash = mlv_hash_bytes(hash, &processing->AgX, sizeof(processing->AgX));
    hash = mlv_hash_bytes(hash, &processing->filter_on, sizeof(processing->filter_on));
    hash = mlv_hash_filter_object(hash, processing->filter);
    hash = mlv_hash_bytes(hash, &processing->lut_on, sizeof(processing->lut_on));
    hash = mlv_hash_lut(hash, processing->lut);
    hash = mlv_hash_bytes(hash, &processing->wbFindActive, sizeof(processing->wbFindActive));
    hash = mlv_hash_bytes(hash, &processing->wbR, sizeof(processing->wbR));
    hash = mlv_hash_bytes(hash, &processing->wbG, sizeof(processing->wbG));
    hash = mlv_hash_bytes(hash, &processing->wbB, sizeof(processing->wbB));
    if (processing->image_profile)
    {
        hash = mlv_hash_bytes(hash, &processing->image_profile->gamma_power, sizeof(processing->image_profile->gamma_power));
        hash = mlv_hash_bytes(hash, &processing->image_profile->tonemap_function, sizeof(processing->image_profile->tonemap_function));
        hash = mlv_hash_bytes(hash, &processing->image_profile->allow_creative_adjustments, sizeof(processing->image_profile->allow_creative_adjustments));
        hash = mlv_hash_bytes(hash, &processing->image_profile->colour_gamut, sizeof(processing->image_profile->colour_gamut));
        hash = mlv_hash_c_string(hash, processing->image_profile->transfer_function);
    }
    else
    {
        hash = mlv_hash_bytes(hash, "", 1);
    }
    hash = mlv_hash_bytes(hash, &processing->black_level, sizeof(processing->black_level));
    hash = mlv_hash_bytes(hash, &processing->white_level, sizeof(processing->white_level));
    hash = mlv_hash_bytes(hash, &processing->highlight_reconstruction, sizeof(processing->highlight_reconstruction));
    hash = mlv_hash_bytes(hash, &processing->highest_green, sizeof(processing->highest_green));
    hash = mlv_hash_bytes(hash, &processing->highest_green_gradient, sizeof(processing->highest_green_gradient));
    hash = mlv_hash_bytes(hash, &processing->highest_green_diso, sizeof(processing->highest_green_diso));
    hash = mlv_hash_bytes(hash, &processing->highest_green_gradient_diso, sizeof(processing->highest_green_gradient_diso));
    hash = mlv_hash_bytes(hash, processing->gcurve_y, sizeof(processing->gcurve_y));
    hash = mlv_hash_bytes(hash, processing->gcurve_r, sizeof(processing->gcurve_r));
    hash = mlv_hash_bytes(hash, processing->gcurve_g, sizeof(processing->gcurve_g));
    hash = mlv_hash_bytes(hash, processing->gcurve_b, sizeof(processing->gcurve_b));
    hash = mlv_hash_bytes(hash, processing->hue_vs_hue, sizeof(processing->hue_vs_hue));
    hash = mlv_hash_bytes(hash, processing->hue_vs_saturation, sizeof(processing->hue_vs_saturation));
    hash = mlv_hash_bytes(hash, processing->hue_vs_luma, sizeof(processing->hue_vs_luma));
    hash = mlv_hash_bytes(hash, processing->luma_vs_saturation, sizeof(processing->luma_vs_saturation));
    hash = mlv_hash_bytes(hash, &processing->hue_vs_hue_used, sizeof(processing->hue_vs_hue_used));
    hash = mlv_hash_bytes(hash, &processing->hue_vs_saturation_used, sizeof(processing->hue_vs_saturation_used));
    hash = mlv_hash_bytes(hash, &processing->hue_vs_luma_used, sizeof(processing->hue_vs_luma_used));
    hash = mlv_hash_bytes(hash, &processing->luma_vs_saturation_used, sizeof(processing->luma_vs_saturation_used));
    hash = mlv_hash_bytes(hash, &processing->toning_dry, sizeof(processing->toning_dry));
    hash = mlv_hash_bytes(hash, processing->toning_wet, sizeof(processing->toning_wet));
    hash = mlv_hash_bytes(hash, processing->cam_matrix, sizeof(processing->cam_matrix));
    hash = mlv_hash_bytes(hash, processing->cam_matrix_A, sizeof(processing->cam_matrix_A));
    hash = mlv_hash_bytes(hash, processing->proper_wb_matrix, sizeof(processing->proper_wb_matrix));
    hash = mlv_hash_bytes(hash, processing->final_matrix, sizeof(processing->final_matrix));
    hash = mlv_hash_bytes(hash, &processing->cs_zone.use_cs, sizeof(processing->cs_zone.use_cs));
    hash = mlv_hash_bytes(hash, &processing->cs_zone.chroma_blur_radius, sizeof(processing->cs_zone.chroma_blur_radius));
    hash = mlv_hash_bytes(hash, &processing->shadows_highlights.highlights, sizeof(processing->shadows_highlights.highlights));
    hash = mlv_hash_bytes(hash, &processing->shadows_highlights.shadows, sizeof(processing->shadows_highlights.shadows));
    hash = mlv_hash_bytes(hash, &processing->kelvin, sizeof(processing->kelvin));
    hash = mlv_hash_bytes(hash, &processing->wb_tint, sizeof(processing->wb_tint));
    hash = mlv_hash_bytes(hash, &processing->exposure_stops, sizeof(processing->exposure_stops));
    hash = mlv_hash_bytes(hash, &processing->saturation, sizeof(processing->saturation));
    hash = mlv_hash_bytes(hash, &processing->vibrance, sizeof(processing->vibrance));
    hash = mlv_hash_bytes(hash, &processing->contrast, sizeof(processing->contrast));
    hash = mlv_hash_bytes(hash, &processing->pivot, sizeof(processing->pivot));
    hash = mlv_hash_bytes(hash, &processing->clarity, sizeof(processing->clarity));
    hash = mlv_hash_bytes(hash, &processing->light_contrast_factor, sizeof(processing->light_contrast_factor));
    hash = mlv_hash_bytes(hash, &processing->light_contrast_range, sizeof(processing->light_contrast_range));
    hash = mlv_hash_bytes(hash, &processing->dark_contrast_factor, sizeof(processing->dark_contrast_factor));
    hash = mlv_hash_bytes(hash, &processing->dark_contrast_range, sizeof(processing->dark_contrast_range));
    hash = mlv_hash_bytes(hash, &processing->highlight_hue, sizeof(processing->highlight_hue));
    hash = mlv_hash_bytes(hash, &processing->midtone_hue, sizeof(processing->midtone_hue));
    hash = mlv_hash_bytes(hash, &processing->shadow_hue, sizeof(processing->shadow_hue));
    hash = mlv_hash_bytes(hash, &processing->highlight_sat, sizeof(processing->highlight_sat));
    hash = mlv_hash_bytes(hash, &processing->midtone_sat, sizeof(processing->midtone_sat));
    hash = mlv_hash_bytes(hash, &processing->shadow_sat, sizeof(processing->shadow_sat));
    hash = mlv_hash_bytes(hash, &processing->gamma_power, sizeof(processing->gamma_power));
    hash = mlv_hash_bytes(hash, &processing->lighten, sizeof(processing->lighten));
    hash = mlv_hash_bytes(hash, &processing->sharpen, sizeof(processing->sharpen));
    hash = mlv_hash_bytes(hash, &processing->sharpen_bias, sizeof(processing->sharpen_bias));
    hash = mlv_hash_bytes(hash, &processing->sh_masking, sizeof(processing->sh_masking));
    hash = mlv_hash_bytes(hash, processing->wb_multipliers, sizeof(processing->wb_multipliers));
    hash = mlv_hash_bytes(hash, &processing->transformation, sizeof(processing->transformation));
    if (processing->dual_iso)
    {
        hash = mlv_hash_bytes(hash, processing->dual_iso, sizeof(*processing->dual_iso));
    }
    else
    {
        hash = mlv_hash_bytes(hash, "", 1);
    }
    hash = mlv_hash_bytes(hash, &processing->denoiserWindow, sizeof(processing->denoiserWindow));
    hash = mlv_hash_bytes(hash, &processing->denoiserStrength, sizeof(processing->denoiserStrength));
    hash = mlv_hash_bytes(hash, &processing->rbfDenoiserLuma, sizeof(processing->rbfDenoiserLuma));
    hash = mlv_hash_bytes(hash, &processing->rbfDenoiserChroma, sizeof(processing->rbfDenoiserChroma));
    hash = mlv_hash_bytes(hash, &processing->rbfDenoiserRange, sizeof(processing->rbfDenoiserRange));
    hash = mlv_hash_bytes(hash, &processing->grainStrength, sizeof(processing->grainStrength));
    hash = mlv_hash_bytes(hash, &processing->grainLumaWeight, sizeof(processing->grainLumaWeight));
    hash = mlv_hash_bytes(hash, &processing->gradient_exposure_stops, sizeof(processing->gradient_exposure_stops));
    hash = mlv_hash_bytes(hash, &processing->gradient_contrast, sizeof(processing->gradient_contrast));
    hash = mlv_hash_bytes(hash, &processing->gradient_enable, sizeof(processing->gradient_enable));
    if (processing->gradient_mask)
    {
        size_t mask_pixels = (size_t)getMlvWidth(video) * (size_t)getMlvHeight(video);
        hash = mlv_hash_sampled_bytes(hash, processing->gradient_mask, mask_pixels * sizeof(uint16_t));
    }
    else
    {
        hash = mlv_hash_bytes(hash, "", 1);
    }
    hash = mlv_hash_bytes(hash, &processing->vignette_strength, sizeof(processing->vignette_strength));
    if (processing->vignette_mask)
    {
        size_t mask_pixels = (size_t)getMlvWidth(video) * (size_t)getMlvHeight(video);
        hash = mlv_hash_sampled_bytes(hash, processing->vignette_mask, mask_pixels * sizeof(float));
    }
    else
    {
        hash = mlv_hash_bytes(hash, "", 1);
    }
    hash = mlv_hash_bytes(hash, &processing->use_cam_matrix, sizeof(processing->use_cam_matrix));
    hash = mlv_hash_bytes(hash, &processing->colour_gamut, sizeof(processing->colour_gamut));
    hash = mlv_hash_bytes(hash, &processing->tonemap_function, sizeof(processing->tonemap_function));
    hash = mlv_hash_bytes(hash, &processing->colour_space_tag, sizeof(processing->colour_space_tag));
    hash = mlv_hash_bytes(hash, &processing->allow_creative_adjustments, sizeof(processing->allow_creative_adjustments));
    hash = mlv_hash_bytes(hash, &processing->ca_desaturate, sizeof(processing->ca_desaturate));
    hash = mlv_hash_bytes(hash, &processing->ca_radius, sizeof(processing->ca_radius));
    hash = mlv_hash_bytes(hash, &processing->transfer_split, sizeof(processing->transfer_split));
    hash = mlv_hash_bytes(hash, &processing->transfer_split_value, sizeof(processing->transfer_split_value));
    hash = mlv_hash_c_string(hash, processing->transfer_function_string);
    hash = mlv_hash_c_string(hash, processing->transfer_function_string_formatted);

    return hash;
}

static uint64_t mlv_processed_frame_signature_from_state(uint64_t stateSignature,
                                                         uint64_t frameIndex)
{
    return mlv_hash_bytes(stateSignature, &frameIndex, sizeof(frameIndex));
}

static uint64_t mlv_processed_frame_signature(mlvObject_t * video, uint64_t frameIndex)
{
    return mlv_processed_frame_signature_from_state(
        mlv_processed_frame_state_signature(video),
        frameIndex);
}

/* Phase 4A: scale-aware variant. Always reachable from Scaled* entry
 * points; the existing scale-1 callers continue to use the non-scaled
 * helpers above and stay byte-identical with their previous behaviour. */
static uint64_t mlv_processed_frame_signature_with_scale(mlvObject_t * video,
                                                         uint64_t frameIndex,
                                                         int scaleFactor)
{
    return mlv_processed_frame_signature_from_state(
        mlv_processed_frame_state_signature_with_scale(video, scaleFactor),
        frameIndex);
}

/* Round-4 item R4-1: cheap fingerprint over the ONLY hashed state a render
 * itself can mutate -- the llrawproc status words (fpm_status/bpm_status flip
 * on the first frame, see the Phase E7 note at the direct8 store) plus the
 * black/white levels that llrawproc dual ISO rewrites and
 * mlv_sync_processing_black_white_levels copies into the processing object.
 * The full signature additionally hashes ~1.1MB of curves/HSL tables that
 * only change through setters between renders, never inside one, so
 * re-hashing all of it at the store site duplicated the entry hash on every
 * frame. If this fingerprint is unchanged across the render the entry
 * signature is still valid at store time; any drift falls back to the full
 * recompute, so a field missing here costs one redundant cache miss on the
 * next lookup, never a stale hit. */
static uint64_t mlv_render_mutable_state_fingerprint(mlvObject_t * video)
{
    uint64_t hash = MLV_FNV1A_OFFSET_BASIS;
    if (!video)
    {
        return hash;
    }
    hash = mlv_hash_bytes(hash, &video->RAWI.raw_info.black_level, sizeof(video->RAWI.raw_info.black_level));
    hash = mlv_hash_bytes(hash, &video->RAWI.raw_info.white_level, sizeof(video->RAWI.raw_info.white_level));
    hash = mlv_hash_llrawproc_state(hash, video->llrawproc);
    processingObject_t * processing = video->processing;
    if (processing)
    {
        hash = mlv_hash_bytes(hash, &processing->black_level, sizeof(processing->black_level));
        hash = mlv_hash_bytes(hash, &processing->white_level, sizeof(processing->white_level));
    }
    return hash;
}

static int mlv_ensure_reusable_buffer(void ** buffer,
                                      uint64_t * capacity_elements,
                                      uint64_t required_elements,
                                      size_t element_size)
{
    if (required_elements == 0)
    {
        return 1;
    }

    if (*buffer && (*capacity_elements >= required_elements))
    {
        return 1;
    }

    if (required_elements > ((uint64_t)SIZE_MAX / element_size))
    {
        return 0;
    }

    void * resized = realloc(*buffer, (size_t)required_elements * element_size);
    if (!resized)
    {
        return 0;
    }

    *buffer = resized;
    *capacity_elements = required_elements;
    return 1;
}

static uint16_t * mlv_ensure_u16_buffer(uint16_t ** buffer, uint64_t * capacity_words, uint64_t required_words)
{
    if (!mlv_ensure_reusable_buffer((void **)buffer, capacity_words, required_words, sizeof(uint16_t)))
    {
        return NULL;
    }
    return *buffer;
}

static uint16_t * mlv_ensure_thread_u16_buffer(uint64_t required_words)
{
    static MLV_THREAD_LOCAL uint16_t * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_words = 0;
    return mlv_ensure_u16_buffer(&tls_buffer, &tls_capacity_words, required_words);
}

static uint16_t * mlv_ensure_thread_rgb_u16_buffer(uint64_t required_words)
{
    static MLV_THREAD_LOCAL uint16_t * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_words = 0;
    return mlv_ensure_u16_buffer(&tls_buffer, &tls_capacity_words, required_words);
}

static uint8_t * mlv_ensure_u8_buffer(uint8_t ** buffer, uint64_t * capacity_bytes, uint64_t required_bytes)
{
    if (!mlv_ensure_reusable_buffer((void **)buffer, capacity_bytes, required_bytes, sizeof(uint8_t)))
    {
        return NULL;
    }
    return *buffer;
}

static uint8_t * mlv_ensure_thread_u8_buffer(uint64_t required_bytes)
{
    static MLV_THREAD_LOCAL uint8_t * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_bytes = 0;
    return mlv_ensure_u8_buffer(&tls_buffer, &tls_capacity_bytes, required_bytes);
}

/* Round-4 item 2: dedicated TLS slot for the scaled-input frame of the
 * direct8 / worker-indirect render helpers. The scaled-render cores ensure
 * mlv_ensure_thread_rgb_u16_buffer for their OWN intermediates (e.g. the x2
 * quarter core's q_rgb) and then upscale INTO the caller's output buffer;
 * handing those cores the same TLS slot as their output self-aliases that
 * upscale (read and write on one allocation) - the x2 direct8 input
 * rendered near-flat garbage (3 distinct values across 100k pixels at
 * threads=1, partial fills at threads=16 = the VIEWED banding). A separate
 * slot makes caller-output and core-intermediate disjoint by construction. */
static uint16_t * mlv_ensure_thread_scaled_input_buffer(uint64_t required_words)
{
    static MLV_THREAD_LOCAL uint16_t * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_words = 0;
    return mlv_ensure_u16_buffer(&tls_buffer, &tls_capacity_words, required_words);
}

static int getMlvRawFrameUint16Direct(mlvObject_t * video, uint64_t frameIndex, uint16_t * unpackedFrame);

static void mlv_reset_raw_uint16_prefetch_locked(mlvObject_t * video)
{
    video->raw_uint16_prefetch_request_pending = 0;
    video->raw_uint16_prefetch_request_stride = 1;
    memset(video->raw_uint16_prefetch_slot_state, 0, sizeof(video->raw_uint16_prefetch_slot_state));
    memset(video->raw_uint16_prefetch_slot_frame, 0, sizeof(video->raw_uint16_prefetch_slot_frame));
    memset(video->raw_uint16_prefetch_slot_generation, 0, sizeof(video->raw_uint16_prefetch_slot_generation));
    video->raw_uint16_prefetch_next_slot = 0;
}

static uint16_t * mlv_raw_uint16_prefetch_slot_ptr(mlvObject_t * video, uint32_t slot)
{
    if (!video->raw_uint16_prefetch_cache
        || slot >= MLV_RAW_UINT16_PREFETCH_SLOTS
        || video->raw_uint16_prefetch_slot_words == 0)
    {
        return NULL;
    }

    uint64_t offset = (uint64_t)slot * video->raw_uint16_prefetch_slot_words;
    if (offset + video->raw_uint16_prefetch_slot_words > video->raw_uint16_prefetch_cache_words)
    {
        return NULL;
    }

    return video->raw_uint16_prefetch_cache + offset;
}

static int mlv_ensure_raw_uint16_prefetch_storage(mlvObject_t * video)
{
    uint64_t frame_words = (uint64_t)getMlvWidth(video) * getMlvHeight(video);
    uint64_t total_words = frame_words * MLV_RAW_UINT16_PREFETCH_SLOTS;
    if (frame_words == 0 || (frame_words != 0 && total_words / frame_words != MLV_RAW_UINT16_PREFETCH_SLOTS))
    {
        return 0;
    }

    uint64_t previous_capacity = video->raw_uint16_prefetch_cache_words;
    uint16_t * cache = mlv_ensure_u16_buffer(&video->raw_uint16_prefetch_cache,
                                             &video->raw_uint16_prefetch_cache_words,
                                             total_words);
    if (!cache)
    {
        return 0;
    }

    if (previous_capacity != video->raw_uint16_prefetch_cache_words
        || video->raw_uint16_prefetch_slot_words != frame_words)
    {
        video->raw_uint16_prefetch_slot_words = frame_words;
        mlv_reset_raw_uint16_prefetch_locked(video);
    }

    return 1;
}

static int mlv_raw_uint16_prefetch_find_slot_locked(mlvObject_t * video, uint64_t frameIndex)
{
    for (uint32_t slot = 0; slot < MLV_RAW_UINT16_PREFETCH_SLOTS; ++slot)
    {
        if (video->raw_uint16_prefetch_slot_state[slot] == MLV_RAW_UINT16_PREFETCH_READY
            && video->raw_uint16_prefetch_slot_frame[slot] == frameIndex
            && video->raw_uint16_prefetch_slot_generation[slot] == video->raw_uint16_prefetch_generation)
        {
            return (int)slot;
        }
    }

    return -1;
}

static void mlv_raw_uint16_prefetch_store_frame(mlvObject_t * video,
                                                uint64_t frameIndex,
                                                const uint16_t * frameData)
{
    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    if (!mlv_ensure_raw_uint16_prefetch_storage(video))
    {
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
        return;
    }

    int slot = mlv_raw_uint16_prefetch_find_slot_locked(video, frameIndex);
    if (slot < 0)
    {
        slot = (int)video->raw_uint16_prefetch_next_slot;
        video->raw_uint16_prefetch_next_slot =
            (video->raw_uint16_prefetch_next_slot + 1) % MLV_RAW_UINT16_PREFETCH_SLOTS;
    }

    uint16_t * slotBuffer = mlv_raw_uint16_prefetch_slot_ptr(video, (uint32_t)slot);
    if (!slotBuffer)
    {
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
        return;
    }

    memcpy(slotBuffer,
           frameData,
           (size_t)video->raw_uint16_prefetch_slot_words * sizeof(uint16_t));
    video->raw_uint16_prefetch_slot_state[slot] = MLV_RAW_UINT16_PREFETCH_READY;
    video->raw_uint16_prefetch_slot_frame[slot] = frameIndex;
    video->raw_uint16_prefetch_slot_generation[slot] = video->raw_uint16_prefetch_generation;
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
}

static int mlv_raw_uint16_prefetch_try_copy(mlvObject_t * video,
                                            uint64_t frameIndex,
                                            uint16_t * unpackedFrame)
{
    int hit = 0;
    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    int slot = mlv_raw_uint16_prefetch_find_slot_locked(video, frameIndex);
    if (slot >= 0)
    {
        uint16_t * slotBuffer = mlv_raw_uint16_prefetch_slot_ptr(video, (uint32_t)slot);
        if (slotBuffer)
        {
            memcpy(unpackedFrame,
                   slotBuffer,
                   (size_t)video->raw_uint16_prefetch_slot_words * sizeof(uint16_t));
            hit = 1;
        }
    }
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    return hit;
}

static int mlv_start_raw_uint16_prefetch_thread(mlvObject_t * video);

static void mlv_raw_uint16_prefetch_note_request(mlvObject_t * video, uint64_t frameIndex)
{
    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);

    if (!video->raw_uint16_prefetch_thread_started)
    {
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
        if (!mlv_start_raw_uint16_prefetch_thread(video))
        {
            return;
        }
        pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    }

    uint32_t requestStride = 1;
    if (frameIndex < video->raw_uint16_prefetch_last_request_frame)
    {
        ++video->raw_uint16_prefetch_generation;
        mlv_reset_raw_uint16_prefetch_locked(video);
    }
    else if (frameIndex > video->raw_uint16_prefetch_last_request_frame)
    {
        uint64_t delta = frameIndex - video->raw_uint16_prefetch_last_request_frame;
        if (delta > MLV_RAW_UINT16_PREFETCH_MAX_PREDICTIVE_STRIDE)
        {
            ++video->raw_uint16_prefetch_generation;
            mlv_reset_raw_uint16_prefetch_locked(video);
        }
        else
        {
            requestStride = (uint32_t)MAX(delta, 1);
        }
    }

    video->raw_uint16_prefetch_last_request_frame = frameIndex;
    video->raw_uint16_prefetch_request_frame = frameIndex;
    video->raw_uint16_prefetch_request_stride = requestStride;
    video->raw_uint16_prefetch_request_pending = 1;
    pthread_cond_signal(&video->raw_uint16_prefetch_cond);
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
}

static void * mlv_raw_uint16_prefetch_thread_main(void * opaque)
{
    mlvObject_t * video = (mlvObject_t *)opaque;

    while (1)
    {
        pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
        while (!video->raw_uint16_prefetch_stop
               && !video->raw_uint16_prefetch_request_pending)
        {
            pthread_cond_wait(&video->raw_uint16_prefetch_cond, &video->raw_uint16_prefetch_mutex);
        }

        if (video->raw_uint16_prefetch_stop)
        {
            pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
            break;
        }

        if (!mlv_ensure_raw_uint16_prefetch_storage(video))
        {
            video->raw_uint16_prefetch_request_pending = 0;
            pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
            continue;
        }

        uint64_t baseFrame = video->raw_uint16_prefetch_request_frame;
        uint32_t requestStride = video->raw_uint16_prefetch_request_stride;
        uint32_t generation = video->raw_uint16_prefetch_generation;
        video->raw_uint16_prefetch_request_pending = 0;
        video->raw_uint16_prefetch_worker_busy = 1;
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);

        if (requestStride == 0)
        {
            requestStride = 1;
        }

        const uint32_t lookahead =
            mlv_raw_uint16_prefetch_lookahead_for_request(video);

        for (uint32_t offset = 1; offset <= lookahead; ++offset)
        {
            /* Round-4 item 1: drop-frame playback advances by an alternating
             * 1..3-frame pattern, so extrapolating the LAST delta as a fixed
             * stride misses most next requests (measured 36-64% hit rate at
             * x1/x2). A contiguous window contains every possible next
             * request for small strides; keep the stride extrapolation only
             * for genuinely large jumps (fast scrubs). */
            uint64_t targetFrame =
                (requestStride <= 4)
                ? baseFrame + offset
                : baseFrame + ((uint64_t)offset * requestStride);
            if (targetFrame >= getMlvFrames(video))
            {
                break;
            }

            pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
            if (video->raw_uint16_prefetch_stop
                || generation != video->raw_uint16_prefetch_generation)
            {
                pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
                break;
            }

            if (mlv_raw_uint16_prefetch_find_slot_locked(video, targetFrame) >= 0)
            {
                pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
                continue;
            }

            uint32_t slot = video->raw_uint16_prefetch_next_slot;
            video->raw_uint16_prefetch_next_slot =
                (video->raw_uint16_prefetch_next_slot + 1) % MLV_RAW_UINT16_PREFETCH_SLOTS;
            uint16_t * slotBuffer = mlv_raw_uint16_prefetch_slot_ptr(video, slot);
            if (!slotBuffer)
            {
                pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
                continue;
            }

            video->raw_uint16_prefetch_slot_state[slot] = MLV_RAW_UINT16_PREFETCH_DECODING;
            video->raw_uint16_prefetch_slot_frame[slot] = targetFrame;
            video->raw_uint16_prefetch_slot_generation[slot] = generation;
            pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);

            int decodeOk = (getMlvRawFrameUint16Direct(video, targetFrame, slotBuffer) == 0);

            pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
            if (!decodeOk)
            {
                ++video->raw_uint16_prefetch_decode_failures;
            }
            if (slot < MLV_RAW_UINT16_PREFETCH_SLOTS
                && video->raw_uint16_prefetch_slot_frame[slot] == targetFrame
                && video->raw_uint16_prefetch_slot_generation[slot] == generation)
            {
                video->raw_uint16_prefetch_slot_state[slot] =
                    (decodeOk && !video->raw_uint16_prefetch_stop
                     && generation == video->raw_uint16_prefetch_generation)
                    ? MLV_RAW_UINT16_PREFETCH_READY
                    : MLV_RAW_UINT16_PREFETCH_EMPTY;
            }
            pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
        }

        pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
        video->raw_uint16_prefetch_worker_busy = 0;
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    }

    return NULL;
}

static int mlv_start_raw_uint16_prefetch_thread(mlvObject_t * video)
{
    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    if (video->raw_uint16_prefetch_thread_started)
    {
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
        return 1;
    }

    /* Hold the mutex across pthread_create so the started flag only publishes
     * after the thread actually exists; otherwise a concurrent note_request
     * could signal a cond for a thread that failed to spawn. */
    int create_rc = pthread_create(&video->raw_uint16_prefetch_thread,
                                   NULL,
                                   mlv_raw_uint16_prefetch_thread_main,
                                   video);
    if (create_rc == 0)
    {
        video->raw_uint16_prefetch_thread_started = 1;
    }
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    return create_rc == 0 ? 1 : 0;
}

static void mlv_reset_processed_frame_16bit_cache(mlvObject_t * video)
{
    video->processed_16bit_cache_next_slot = 0;
    memset(video->processed_16bit_cache_active, 0, sizeof(video->processed_16bit_cache_active));
    memset(video->processed_16bit_cache_frame, 0, sizeof(video->processed_16bit_cache_frame));
    memset(video->processed_16bit_cache_threads, 0, sizeof(video->processed_16bit_cache_threads));
    memset(video->processed_16bit_cache_signature, 0, sizeof(video->processed_16bit_cache_signature));
    /* Phase 4A: scale lane resets to 0 (the "unset" sentinel). Real entries
     * always store a normalised 1, 2, 4, or 8 so a zero lane never matches
     * any live request. */
    memset(video->processed_16bit_cache_scale, 0, sizeof(video->processed_16bit_cache_scale));
    /* Phase 4B: clear the layout unit so the next prepare picks up the
     * new layout fresh. */
    video->processed_16bit_cache_unit_words = 0;
}

static uint16_t * mlv_processed_frame_16bit_cache_slot(mlvObject_t * video, uint32_t slot, uint64_t rgb_frame_words)
{
    if (!video->rgb_processed_frame_cache_16bit
        || slot >= MLV_PROCESSED_16BIT_CACHE_SLOTS)
    {
        return NULL;
    }

    uint64_t offset = (uint64_t)slot * rgb_frame_words;
    if (rgb_frame_words != 0 && offset / rgb_frame_words != slot)
    {
        return NULL;
    }
    if (video->rgb_processed_frame_cache_16bit_words < offset + rgb_frame_words)
    {
        return NULL;
    }

    return video->rgb_processed_frame_cache_16bit + offset;
}

static int mlv_find_processed_frame_16bit_cache_slot_with_scale(mlvObject_t * video,
                                                                uint64_t frameIndex,
                                                                int threads,
                                                                uint64_t signature,
                                                                int scaleFactor)
{
    const int normalizedScale = mlv_normalize_playback_scale_factor(scaleFactor);
    for (uint32_t slot = 0; slot < MLV_PROCESSED_16BIT_CACHE_SLOTS; ++slot)
    {
        if (video->processed_16bit_cache_active[slot]
            && video->processed_16bit_cache_frame[slot] == frameIndex
            && video->processed_16bit_cache_threads[slot] == threads
            && video->processed_16bit_cache_signature[slot] == signature
            && video->processed_16bit_cache_scale[slot] == normalizedScale)
        {
            return (int)slot;
        }
    }

    return -1;
}

static int mlv_find_processed_frame_16bit_cache_slot(mlvObject_t * video,
                                                     uint64_t frameIndex,
                                                     int threads,
                                                     uint64_t signature)
{
    return mlv_find_processed_frame_16bit_cache_slot_with_scale(video,
                                                                frameIndex,
                                                                threads,
                                                                signature,
                                                                1);
}

static uint16_t * mlv_prepare_processed_frame_16bit_cache(mlvObject_t * video,
                                                          uint64_t rgb_frame_words)
{
    uint64_t total_words = rgb_frame_words * MLV_PROCESSED_16BIT_CACHE_SLOTS;
    if (rgb_frame_words != 0 && total_words / rgb_frame_words != MLV_PROCESSED_16BIT_CACHE_SLOTS)
    {
        mlv_reset_processed_frame_16bit_cache(video);
        return NULL;
    }

    /* Phase 4B: when rgb_frame_words differs from the layout unit used by
     * existing slots, the slot offsets are stale (one scale's slot 1
     * overlaps another scale's slot 0). Reset so the next store
     * re-establishes the layout. */
    if (video->processed_16bit_cache_unit_words != 0
        && video->processed_16bit_cache_unit_words != rgb_frame_words)
    {
        mlv_reset_processed_frame_16bit_cache(video);
    }

    uint64_t previous_capacity = video->rgb_processed_frame_cache_16bit_words;
    uint16_t * cache = mlv_ensure_u16_buffer(&video->rgb_processed_frame_cache_16bit,
                                             &video->rgb_processed_frame_cache_16bit_words,
                                             total_words);
    if (!cache)
    {
        mlv_reset_processed_frame_16bit_cache(video);
        return NULL;
    }

    if (previous_capacity != video->rgb_processed_frame_cache_16bit_words)
    {
        mlv_reset_processed_frame_16bit_cache(video);
    }

    /* Record the layout unit so subsequent calls can detect a size change. */
    video->processed_16bit_cache_unit_words = rgb_frame_words;
    return cache;
}

static void mlv_store_processed_frame_16bit_cache_with_scale(mlvObject_t * video,
                                                             uint64_t frameIndex,
                                                             int threads,
                                                             uint64_t signature,
                                                             const uint16_t * frame_data,
                                                             uint64_t rgb_frame_words,
                                                             int scaleFactor)
{
    const double store_start = mlv_stage_timing_now();
    uint16_t * cache = mlv_prepare_processed_frame_16bit_cache(video, rgb_frame_words);
    if (!cache)
    {
        g_mlv_last_processed16_cache_store_ms += (mlv_stage_timing_now() - store_start) * 1000.0;
        return;
    }

    const int normalizedScale = mlv_normalize_playback_scale_factor(scaleFactor);
    int slot = mlv_find_processed_frame_16bit_cache_slot_with_scale(video,
                                                                    frameIndex,
                                                                    threads,
                                                                    signature,
                                                                    normalizedScale);
    if (slot < 0)
    {
        slot = (int)video->processed_16bit_cache_next_slot;
        video->processed_16bit_cache_next_slot = (video->processed_16bit_cache_next_slot + 1) % MLV_PROCESSED_16BIT_CACHE_SLOTS;
    }

    uint16_t * slot_buffer = mlv_processed_frame_16bit_cache_slot(video, (uint32_t)slot, rgb_frame_words);
    if (!slot_buffer)
    {
        mlv_reset_processed_frame_16bit_cache(video);
        g_mlv_last_processed16_cache_store_ms += (mlv_stage_timing_now() - store_start) * 1000.0;
        return;
    }

    memcpy(slot_buffer, frame_data, (size_t)rgb_frame_words * sizeof(uint16_t));
    video->processed_16bit_cache_active[slot] = 1;
    video->processed_16bit_cache_frame[slot] = frameIndex;
    video->processed_16bit_cache_threads[slot] = threads;
    video->processed_16bit_cache_signature[slot] = signature;
    video->processed_16bit_cache_scale[slot] = normalizedScale;
    g_mlv_last_processed16_cache_store_ms += (mlv_stage_timing_now() - store_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed16_cache_store",
                                  frameIndex,
                                  g_mlv_last_processed16_cache_store_ms);
}

static void mlv_store_processed_frame_16bit_cache(mlvObject_t * video,
                                                  uint64_t frameIndex,
                                                  int threads,
                                                  uint64_t signature,
                                                  const uint16_t * frame_data,
                                                  uint64_t rgb_frame_words)
{
    mlv_store_processed_frame_16bit_cache_with_scale(video,
                                                     frameIndex,
                                                     threads,
                                                     signature,
                                                     frame_data,
                                                     rgb_frame_words,
                                                     1);
}

static void mlv_reset_processed_frame_8bit_cache_locked(mlvObject_t * video)
{
    video->current_processed_frame_8bit_active = 0;
    video->current_processed_frame_8bit_signature = 0;
    video->current_processed_frame_8bit = 0;
    video->current_processed_frame_8bit_threads = 0;
    video->processed_8bit_cache_next_slot = 0;
    memset(video->processed_8bit_cache_active, 0, sizeof(video->processed_8bit_cache_active));
    memset(video->processed_8bit_cache_frame, 0, sizeof(video->processed_8bit_cache_frame));
    memset(video->processed_8bit_cache_threads, 0, sizeof(video->processed_8bit_cache_threads));
    memset(video->processed_8bit_cache_signature, 0, sizeof(video->processed_8bit_cache_signature));
    /* Phase 4A: scale lane resets to 0 (no live request matches a 0 lane). */
    memset(video->processed_8bit_cache_scale, 0, sizeof(video->processed_8bit_cache_scale));
    memset(video->processed_8bit_cache_phase4b_path, 0, sizeof(video->processed_8bit_cache_phase4b_path));
    memset(video->processed_8bit_cache_phase4b_y_crop_rows, 0, sizeof(video->processed_8bit_cache_phase4b_y_crop_rows));
    memset(video->processed_8bit_cache_state, 0, sizeof(video->processed_8bit_cache_state));
    memset(video->processed_8bit_cache_prefetched, 0, sizeof(video->processed_8bit_cache_prefetched));
    memset(video->processed_8bit_cache_generation, 0, sizeof(video->processed_8bit_cache_generation));
    /* Phase 4B: clear the per-call unit size so the next prepare picks up
     * the new layout fresh. */
    video->processed_8bit_cache_unit_size = 0;
}

static void mlv_reset_processed_frame_8bit_cache(mlvObject_t * video)
{
    mlv_reset_processed_frame_8bit_cache_locked(video);
}

static uint8_t * mlv_processed_frame_8bit_cache_slot(mlvObject_t * video, uint32_t slot, uint64_t rgb_frame_size)
{
    if (!video->rgb_processed_current_frame_8bit
        || slot >= MLV_PROCESSED_8BIT_CACHE_SLOTS)
    {
        return NULL;
    }

    uint64_t offset = (uint64_t)slot * rgb_frame_size;
    if (rgb_frame_size != 0 && offset / rgb_frame_size != slot)
    {
        return NULL;
    }
    if (video->rgb_processed_current_frame_8bit_bytes < offset + rgb_frame_size)
    {
        return NULL;
    }

    return video->rgb_processed_current_frame_8bit + offset;
}

static int mlv_processed_frame_8bit_cache_slot_matches_locked_with_scale(
    mlvObject_t * video,
    uint32_t slot,
    uint64_t frameIndex,
    int threads,
    uint64_t signature,
    uint32_t generation,
    int scaleFactor)
{
    const int normalizedScale = mlv_normalize_playback_scale_factor(scaleFactor);
    return slot < MLV_PROCESSED_8BIT_CACHE_SLOTS
        && video->processed_8bit_cache_frame[slot] == frameIndex
        && video->processed_8bit_cache_threads[slot] == threads
        && video->processed_8bit_cache_signature[slot] == signature
        && video->processed_8bit_cache_generation[slot] == generation
        && video->processed_8bit_cache_scale[slot] == normalizedScale;
}

static int mlv_processed_frame_8bit_cache_slot_matches_locked(mlvObject_t * video,
                                                              uint32_t slot,
                                                              uint64_t frameIndex,
                                                              int threads,
                                                              uint64_t signature,
                                                              uint32_t generation)
{
    return mlv_processed_frame_8bit_cache_slot_matches_locked_with_scale(video,
                                                                         slot,
                                                                         frameIndex,
                                                                         threads,
                                                                         signature,
                                                                         generation,
                                                                         1);
}

static int mlv_find_processed_frame_8bit_cache_slot_locked_with_scale(mlvObject_t * video,
                                                                      uint64_t frameIndex,
                                                                      int threads,
                                                                      uint64_t signature,
                                                                      int scaleFactor)
{
    for (uint32_t slot = 0; slot < MLV_PROCESSED_8BIT_CACHE_SLOTS; ++slot)
    {
        if (video->processed_8bit_cache_active[slot]
            && video->processed_8bit_cache_state[slot] == MLV_PROCESSED_8BIT_PREFETCH_READY
            && mlv_processed_frame_8bit_cache_slot_matches_locked_with_scale(
                   video,
                   slot,
                   frameIndex,
                   threads,
                   signature,
                   video->processed8_prefetch_generation,
                   scaleFactor))
        {
            return (int)slot;
        }
    }

    return -1;
}

static int mlv_find_processed_frame_8bit_cache_slot_locked(mlvObject_t * video,
                                                           uint64_t frameIndex,
                                                           int threads,
                                                           uint64_t signature)
{
    return mlv_find_processed_frame_8bit_cache_slot_locked_with_scale(video,
                                                                      frameIndex,
                                                                      threads,
                                                                      signature,
                                                                      1);
}

static int mlv_processed_frame_8bit_cache_contains_locked_with_scale(mlvObject_t * video,
                                                                     uint64_t frameIndex,
                                                                     int threads,
                                                                     uint64_t signature,
                                                                     uint32_t generation,
                                                                     int scaleFactor)
{
    for (uint32_t slot = 0; slot < MLV_PROCESSED_8BIT_CACHE_SLOTS; ++slot)
    {
        if (mlv_processed_frame_8bit_cache_slot_matches_locked_with_scale(video,
                                                                          slot,
                                                                          frameIndex,
                                                                          threads,
                                                                          signature,
                                                                          generation,
                                                                          scaleFactor)
            && (video->processed_8bit_cache_state[slot] == MLV_PROCESSED_8BIT_PREFETCH_READY
                || video->processed_8bit_cache_state[slot] == MLV_PROCESSED_8BIT_PREFETCH_RENDERING))
        {
            return 1;
        }
    }

    return 0;
}

static int mlv_processed_frame_8bit_cache_contains_locked(mlvObject_t * video,
                                                          uint64_t frameIndex,
                                                          int threads,
                                                          uint64_t signature,
                                                          uint32_t generation)
{
    return mlv_processed_frame_8bit_cache_contains_locked_with_scale(video,
                                                                     frameIndex,
                                                                     threads,
                                                                     signature,
                                                                     generation,
                                                                     1);
}

static int mlv_next_processed_frame_8bit_cache_slot_locked(mlvObject_t * video)
{
    for (uint32_t offset = 0; offset < MLV_PROCESSED_8BIT_CACHE_SLOTS; ++offset)
    {
        uint32_t slot = (video->processed_8bit_cache_next_slot + offset) % MLV_PROCESSED_8BIT_CACHE_SLOTS;
        if (video->processed_8bit_cache_state[slot] == MLV_PROCESSED_8BIT_PREFETCH_RENDERING
            && !video->processed_8bit_cache_active[slot]
            && video->processed_8bit_cache_frame[slot] == 0
            && video->processed_8bit_cache_threads[slot] == 0
            && video->processed_8bit_cache_signature[slot] == 0)
        {
            video->processed_8bit_cache_state[slot] = MLV_PROCESSED_8BIT_PREFETCH_EMPTY;
            video->processed_8bit_cache_prefetched[slot] = 0;
            video->processed_8bit_cache_generation[slot] = 0;
            video->processed_8bit_cache_phase4b_path[slot] = 0;
            video->processed_8bit_cache_phase4b_y_crop_rows[slot] = 0;
        }

        if (video->processed_8bit_cache_state[slot] != MLV_PROCESSED_8BIT_PREFETCH_RENDERING)
        {
            video->processed_8bit_cache_next_slot = (slot + 1) % MLV_PROCESSED_8BIT_CACHE_SLOTS;
            return (int)slot;
        }
    }

    return -1;
}

static uint8_t * mlv_prepare_processed_frame_8bit_cache_locked(mlvObject_t * video,
                                                               uint64_t rgb_frame_size)
{
    uint64_t total_bytes = rgb_frame_size * MLV_PROCESSED_8BIT_CACHE_SLOTS;
    if (rgb_frame_size != 0 && total_bytes / rgb_frame_size != MLV_PROCESSED_8BIT_CACHE_SLOTS)
    {
        mlv_reset_processed_frame_8bit_cache_locked(video);
        return NULL;
    }

    /* Phase 4B: when rgb_frame_size differs from the size used to lay out
     * existing slots (e.g. a scale=1 store after the buffer was laid out
     * for scale=2), the slot offsets are stale and the data overlaps
     * unrelated entries. Reset so the next store re-establishes the
     * layout. */
    if (video->processed_8bit_cache_unit_size != 0
        && video->processed_8bit_cache_unit_size != rgb_frame_size)
    {
        mlv_reset_processed_frame_8bit_cache_locked(video);
    }

    uint64_t previous_capacity = video->rgb_processed_current_frame_8bit_bytes;
    uint8_t * cache = mlv_ensure_u8_buffer(&video->rgb_processed_current_frame_8bit,
                                           &video->rgb_processed_current_frame_8bit_bytes,
                                           total_bytes);
    if (!cache)
    {
        mlv_reset_processed_frame_8bit_cache_locked(video);
        return NULL;
    }

    if (previous_capacity != video->rgb_processed_current_frame_8bit_bytes)
    {
        mlv_reset_processed_frame_8bit_cache_locked(video);
    }

    /* Record the layout unit so subsequent calls can detect a size change. */
    video->processed_8bit_cache_unit_size = rgb_frame_size;
    return cache;
}

static void mlv_store_processed_frame_8bit_cache_locked_with_scale(mlvObject_t * video,
                                                                   uint64_t frameIndex,
                                                                   int threads,
                                                                   uint64_t signature,
                                                                   const uint8_t * frame_data,
                                                                   uint64_t rgb_frame_size,
                                                                   int update_current_entry,
                                                                   int prefetched,
                                                                   int scaleFactor,
                                                                   int phase4bPath,
                                                                   int phase4bYCropRows)
{
    uint8_t * cache = mlv_prepare_processed_frame_8bit_cache_locked(video, rgb_frame_size);
    if (!cache)
    {
        return;
    }

    const int normalizedScale = mlv_normalize_playback_scale_factor(scaleFactor);
    int slot = mlv_find_processed_frame_8bit_cache_slot_locked_with_scale(video,
                                                                          frameIndex,
                                                                          threads,
                                                                          signature,
                                                                          normalizedScale);
    if (slot < 0)
    {
        slot = mlv_next_processed_frame_8bit_cache_slot_locked(video);
    }

    if (slot < 0)
    {
        return;
    }

    uint8_t * slot_buffer = mlv_processed_frame_8bit_cache_slot(video, (uint32_t)slot, rgb_frame_size);
    if (!slot_buffer)
    {
        mlv_reset_processed_frame_8bit_cache_locked(video);
        return;
    }

    memcpy(slot_buffer, frame_data, (size_t)rgb_frame_size);
    video->processed_8bit_cache_active[slot] = 1;
    video->processed_8bit_cache_frame[slot] = frameIndex;
    video->processed_8bit_cache_threads[slot] = threads;
    video->processed_8bit_cache_signature[slot] = signature;
    video->processed_8bit_cache_scale[slot] = normalizedScale;
    video->processed_8bit_cache_phase4b_path[slot] = phase4bPath;
    video->processed_8bit_cache_phase4b_y_crop_rows[slot] = phase4bYCropRows;
    video->processed_8bit_cache_state[slot] = MLV_PROCESSED_8BIT_PREFETCH_READY;
    video->processed_8bit_cache_prefetched[slot] = prefetched != 0;
    video->processed_8bit_cache_generation[slot] = video->processed8_prefetch_generation;

    if (update_current_entry)
    {
        video->current_processed_frame_8bit_active = 1;
        video->current_processed_frame_8bit = frameIndex;
        video->current_processed_frame_8bit_threads = threads;
        video->current_processed_frame_8bit_signature = signature;
    }
}

static void mlv_store_processed_frame_8bit_cache_locked(mlvObject_t * video,
                                                        uint64_t frameIndex,
                                                        int threads,
                                                        uint64_t signature,
                                                        const uint8_t * frame_data,
                                                        uint64_t rgb_frame_size,
                                                        int update_current_entry,
                                                        int prefetched)
{
    mlv_store_processed_frame_8bit_cache_locked_with_scale(video,
                                                           frameIndex,
                                                           threads,
                                                           signature,
                                                           frame_data,
                                                           rgb_frame_size,
                                                           update_current_entry,
                                                           prefetched,
                                                           1,
                                                           0,
                                                           0);
}

static void mlv_store_processed_frame_8bit_cache(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 int threads,
                                                 uint64_t signature,
                                                 const uint8_t * frame_data,
                                                 uint64_t rgb_frame_size,
                                                 int update_current_entry,
                                                 int prefetched)
{
    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    mlv_store_processed_frame_8bit_cache_locked(video,
                                                frameIndex,
                                                threads,
                                                signature,
                                                frame_data,
                                                rgb_frame_size,
                                                update_current_entry,
                                                prefetched);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
}

static void mlv_store_processed_frame_8bit_cache_with_scale(mlvObject_t * video,
                                                            uint64_t frameIndex,
                                                            int threads,
                                                            uint64_t signature,
                                                            const uint8_t * frame_data,
                                                            uint64_t rgb_frame_size,
                                                            int update_current_entry,
                                                            int prefetched,
                                                            int scaleFactor,
                                                            int phase4bPath,
                                                            int phase4bYCropRows)
{
    /* Aggressive x1/x2/x4 playback is the hot sequential lane now. When the
     * foreground render is not already serving the direct8 path, the cache
     * write mostly adds mutex/copy overhead for a frame that is not being
     * reused immediately. Keep the prefetch-worker store path intact, and
     * let the main render path skip this bookkeeping in the hot lanes. */
    if (prefetched == 0
        && (scaleFactor == 1 || scaleFactor == 2 || scaleFactor == 4)
        && mlvPlaybackAggressivePreviewMode()
        && (scaleFactor == 1
            || !video
            || !video->processing
            || !processingCanUseDirect8BitOutput(video->processing)))
    {
        return;
    }

    if (prefetched == 0
        && scaleFactor == 8
        && mlvPlaybackAggressivePreviewMode())
    {
        return;
    }

    const double store_start = mlv_stage_timing_now();
    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    mlv_store_processed_frame_8bit_cache_locked_with_scale(video,
                                                           frameIndex,
                                                           threads,
                                                           signature,
                                                           frame_data,
                                                           rgb_frame_size,
                                                           update_current_entry,
                                                           prefetched,
                                                           scaleFactor,
                                                           phase4bPath,
                                                           phase4bYCropRows);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    g_mlv_last_processed8_cache_store_ms += (mlv_stage_timing_now() - store_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed8_cache_store",
                                  frameIndex,
                                  g_mlv_last_processed8_cache_store_ms);
}

static void mlv_copy_processed8_prefetch_processing_state(processingObject_t * dst,
                                                          const processingObject_t * src)
{
    if (!dst || !src)
    {
        return;
    }

    dst->exr_mode = src->exr_mode;
    dst->AgX = src->AgX;
    dst->highlight_reconstruction = src->highlight_reconstruction;
    dst->shadows_highlights.highlights = src->shadows_highlights.highlights;
    dst->shadows_highlights.shadows = src->shadows_highlights.shadows;
    dst->contrast = src->contrast;
    dst->clarity = src->clarity;
    dst->saturation = src->saturation;
    dst->vibrance = src->vibrance;
    dst->pivot = src->pivot;
    dst->transformation = src->transformation;
    dst->denoiserStrength = src->denoiserStrength;
    dst->rbfDenoiserLuma = src->rbfDenoiserLuma;
    dst->rbfDenoiserChroma = src->rbfDenoiserChroma;
    dst->grainStrength = src->grainStrength;
    dst->gradient_enable = src->gradient_enable;
    dst->vignette_strength = src->vignette_strength;
    dst->use_cam_matrix = src->use_cam_matrix;
    dst->colour_gamut = src->colour_gamut;
    dst->allow_creative_adjustments = src->allow_creative_adjustments;
    dst->ca_desaturate = src->ca_desaturate;
    dst->ca_radius = src->ca_radius;
    dst->filter_on = src->filter_on;
    dst->lut_on = src->lut_on;
    dst->cs_zone.use_cs = src->cs_zone.use_cs;
    dst->sharpen = src->sharpen;
    memcpy(dst->proper_wb_matrix,
           src->proper_wb_matrix,
           sizeof(dst->proper_wb_matrix));
    memcpy(dst->pre_calc_levels,
           src->pre_calc_levels,
           sizeof(dst->pre_calc_levels));
    memcpy(dst->pre_calc_gamma,
           src->pre_calc_gamma,
           sizeof(dst->pre_calc_gamma));
    memcpy(dst->pre_calc_curve_r,
           src->pre_calc_curve_r,
           sizeof(dst->pre_calc_curve_r));
    memcpy(dst->contrast_curve,
           src->contrast_curve,
           sizeof(dst->contrast_curve));
    memcpy(dst->shadows_highlights.shadow_highlight_curve,
           src->shadows_highlights.shadow_highlight_curve,
           sizeof(dst->shadows_highlights.shadow_highlight_curve));
    memcpy(dst->pre_calc_sat,
           src->pre_calc_sat,
           sizeof(dst->pre_calc_sat));
    memcpy(dst->pre_calc_vibrance,
           src->pre_calc_vibrance,
           sizeof(dst->pre_calc_vibrance));
    memcpy(dst->gcurve_y, src->gcurve_y, sizeof(dst->gcurve_y));
    memcpy(dst->gcurve_r, src->gcurve_r, sizeof(dst->gcurve_r));
    memcpy(dst->gcurve_g, src->gcurve_g, sizeof(dst->gcurve_g));
    memcpy(dst->gcurve_b, src->gcurve_b, sizeof(dst->gcurve_b));

    for (int i = 0; i < 9; ++i)
    {
        if (dst->pre_calc_matrix[i] && src->pre_calc_matrix[i])
        {
            memcpy(dst->pre_calc_matrix[i],
                   src->pre_calc_matrix[i],
                   65536u * sizeof(*dst->pre_calc_matrix[i]));
        }
    }
}

static int mlv_processed_frame_8bit_cache_try_copy(mlvObject_t * video,
                                                   uint64_t frameIndex,
                                                   int threads,
                                                   uint64_t signature,
                                                   int allow_prefetched_hit,
                                                   uint8_t * outputFrame,
                                                   uint64_t rgb_frame_size,
                                                   int scaleFactor,
                                                   int * prefetched,
                                                   int * phase4bPath,
                                                   int * phase4bYCropRows)
{
    int hit = 0;
    if (prefetched)
    {
        *prefetched = 0;
    }
    if (phase4bPath)
    {
        *phase4bPath = 0;
    }
    if (phase4bYCropRows)
    {
        *phase4bYCropRows = 0;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    int slot = mlv_find_processed_frame_8bit_cache_slot_locked_with_scale(video,
                                                                          frameIndex,
                                                                          threads,
                                                                          signature,
                                                                          scaleFactor);
    if (slot >= 0)
    {
        uint8_t * cached_frame = mlv_processed_frame_8bit_cache_slot(video, (uint32_t)slot, rgb_frame_size);
        if (cached_frame)
        {
            const int is_prefetched = video->processed_8bit_cache_prefetched[slot] != 0;
            if (is_prefetched && !allow_prefetched_hit)
            {
                pthread_mutex_unlock(&video->processed8_prefetch_mutex);
                return 0;
            }

            if (outputFrame != cached_frame)
            {
                memcpy(outputFrame, cached_frame, (size_t)rgb_frame_size);
            }
            video->current_processed_frame_8bit_active = 1;
            video->current_processed_frame_8bit = frameIndex;
            video->current_processed_frame_8bit_threads = threads;
            video->current_processed_frame_8bit_signature = signature;
            if (prefetched)
            {
                *prefetched = is_prefetched;
            }
            if (phase4bPath)
            {
                *phase4bPath = video->processed_8bit_cache_phase4b_path[slot];
            }
            if (phase4bYCropRows)
            {
                *phase4bYCropRows = video->processed_8bit_cache_phase4b_y_crop_rows[slot];
            }
            hit = 1;

            /* SDBG_p8cache_hit capture: the exact bytes served on a
             * processed8 cache hit. Inert when MLVAPP_PIPELINE_CAPTURE_DIR
             * is unset. Frozen bytes here while the prefetch-side direct8
             * output advances pinpoint a serve defect; frozen on both sides
             * pinpoint the prefetch render itself. */
            if (mlv_pipeline_capture_should_capture_frame(frameIndex))
            {
                const int hit_scale = mlv_normalize_playback_scale_factor(scaleFactor);
                const int hit_w = (int)getMlvWidth(video) / hit_scale;
                const int hit_h = (int)getMlvHeight(video) / hit_scale;
                mlv_pipeline_capture_meta_t meta;
                memset(&meta, 0, sizeof meta);
                meta.stage = "SDBG_p8cache_hit";
                meta.format = MLV_PIPELINE_FORMAT_UINT8_RGB;
                meta.format_label = "uint8_rgb_processed8_cache_hit";
                meta.width = hit_w;
                meta.height = hit_h;
                meta.bytes_per_line = hit_w * 3;
                meta.bytes_per_pixel = 3;
                meta.channels = 3;
                meta.bit_depth = 8;
                meta.path_label = is_prefetched ? "prefetched" : "foreground_stored";
                meta.settings_hash = (uint64_t)signature;
                mlv_pipeline_capture(frameIndex, cached_frame, &meta);
            }
        }
        else
        {
            mlv_reset_processed_frame_8bit_cache_locked(video);
        }
    }
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);

    return hit;
}

static void mlv_reset_processed8_prefetch_locked(mlvObject_t * video)
{
    video->processed8_prefetch_request_pending = 0;
    video->processed8_prefetch_request_frame = 0;
    video->processed8_prefetch_request_threads = 0;
    video->processed8_prefetch_request_scale = 0;
    mlv_reset_processed_frame_8bit_cache_locked(video);
}

void mlvInvalidateProcessed8PrefetchCache(mlvObject_t * video)
{
    if (!video)
    {
        return;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    /* Generation is checked both before and after a worker render. Bumping it
     * under the same mutex that protects slot publication prevents an
     * in-flight old-generation render from repopulating a cleared cache. */
    ++video->processed8_prefetch_generation;
    mlv_reset_processed8_prefetch_locked(video);
    /* Some user-controlled dual-ISO state deliberately relies on explicit
     * invalidation instead of participating in the steady-state signature.
     * Force the next request to refresh the private processing snapshot even
     * if frame cadence, scale, threads, and signature otherwise still match. */
    video->processed8_prefetch_snapshot_dirty = 1;
    pthread_cond_broadcast(&video->processed8_prefetch_cond);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
}

static int mlv_render_processed_frame8_direct_with_processing(mlvObject_t * video,
                                                              processingObject_t * processing,
                                                              int syncProcessingLevels,
                                                              uint64_t frameIndex,
                                                              uint8_t * outputFrame,
                                                              int threads,
                                                              int scaleFactor,
                                                              int recordTelemetry);
static int mlv_render_processed_frame8_indirect_with_processing(mlvObject_t * video,
                                                                processingObject_t * processing,
                                                                uint64_t frameIndex,
                                                                uint8_t * outputFrame,
                                                                int threads,
                                                                int scaleFactor);
static int mlv_start_processed8_prefetch_thread(mlvObject_t * video);

static void mlv_processed8_prefetch_note_request(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 int threads,
                                                 uint64_t stateSignature,
                                                 int scaleFactor)
{
    pthread_mutex_lock(&video->processed8_prefetch_mutex);

    if (!video->processed8_prefetch_thread_started)
    {
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
        if (!mlv_start_processed8_prefetch_thread(video))
        {
            return;
        }
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
    }

    /* Phase E6: scaleFactor change must invalidate the prefetch generation
     * the same way the state signature already does. (The state signature
     * encodes scale via Phase 4A, so this is in practice subsumed by the
     * stateSignature comparison below; we guard explicitly anyway in case
     * future hash refactors decouple them.) */
    if (video->processed8_prefetch_snapshot_dirty
        || (video->processed8_prefetch_last_request_frame + 1 != frameIndex
         && video->processed8_prefetch_last_request_frame != frameIndex)
        || video->processed8_prefetch_last_request_threads != threads
        || video->processed8_prefetch_last_state_signature != stateSignature
        || video->processed8_prefetch_last_request_scale != scaleFactor)
    {
        ++video->processed8_prefetch_generation;
        mlv_reset_processed8_prefetch_locked(video);
        if (!video->processed8_prefetch_processing || !video->processing)
        {
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            return;
        }

        mlv_copy_processed8_prefetch_processing_state(video->processed8_prefetch_processing,
                                                      video->processing);
        video->processed8_prefetch_snapshot_dirty = 0;
    }
    video->processed8_prefetch_last_request_frame = frameIndex;
    video->processed8_prefetch_last_request_threads = threads;
    video->processed8_prefetch_last_state_signature = stateSignature;
    video->processed8_prefetch_last_request_scale = scaleFactor;
    video->processed8_prefetch_request_frame = frameIndex;
    video->processed8_prefetch_request_threads = threads;
    video->processed8_prefetch_request_scale = scaleFactor;
    video->processed8_prefetch_request_pending = 1;
    pthread_cond_signal(&video->processed8_prefetch_cond);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
}

typedef struct
{
    mlvObject_t * video;
    processingObject_t * processing;
    uint64_t baseFrame;
    int threads;
    uint64_t stateSignature;
    uint32_t generation;
    uint64_t rgb_frame_size;
    uint32_t offsetStart;
    uint32_t offsetStep;
    int scaleFactor; /* Phase E6: must match the render thread's normalizedScale
                      * so the cache slot's `scale` lane lines up with the
                      * lookup. Without this, store-with-scale=4 vs
                      * lookup-with-scale=1 misses every time. */
} mlv_processed8_prefetch_task_t;

static int mlv_processed8_prefetch_lookahead_value(void)
{
    static int cached = -1;
    if (cached < 0)
    {
        int v = MLV_PROCESSED_8BIT_PREFETCH_LOOKAHEAD;
        const char * e = getenv("MLVAPP_PROCESSED8_LOOKAHEAD");
        if (e && e[0]) { int n = atoi(e); if (n > 0) v = n; }
        if (v < 1) v = 1;
        if (v > MLV_PROCESSED_8BIT_CACHE_SLOTS - 1) v = MLV_PROCESSED_8BIT_CACHE_SLOTS - 1;
        cached = v;
    }
    return cached;
}

static void mlv_processed8_prefetch_execute_task(const mlv_processed8_prefetch_task_t * task)
{
    if (!task || !task->video || !task->processing)
    {
        return;
    }

    /* The playback preview policy flags are THREAD-LOCAL. The direct8 gate's
     * local-tone clause only disqualifies when preview mode is active (the
     * default Auto Look Assist preset is the common case), so the gate MUST
     * be evaluated inside the same preview-policy envelope the render uses.
     * Historically this check ran with the worker thread's envelope unset:
     * the gate reported compatible, the direct8 render then failed closed
     * inside the envelope, and the worker silently warmed nothing for
     * look-assist states (wasted decodes, zero stores). Set the envelope for
     * the whole task and restore it on every exit path. */
    const int previous_preview_mode = processingPlaybackPreviewModeEnabled();
    const int previous_aggressive_preview_mode =
        processingPlaybackAggressivePreviewModeEnabled();
    const int previous_preview_scale_factor =
        processingPlaybackPreviewScaleFactor();
    processingSetPlaybackPreviewMode(1);
    processingSetPlaybackAggressivePreviewMode(mlvPlaybackAggressivePreviewMode());
    processingSetPlaybackPreviewScaleFactor(task->scaleFactor);

    /* When the copied processing state is direct8-incompatible, the direct8
     * render cannot produce a frame. Since 2026-06-11 the worker renders
     * those frames through the same indirect processed16->8 composition the
     * foreground uses, so the cache still serves honest hits - but only for
     * states the partial processing-state copy provably represents, and only
     * while MLVAPP_PROCESSED8_PREFETCH_INDIRECT (default on) allows it. Both
     * renders fail closed: a frame is stored only on reported success.
     *
     * Scope (2026-06-11 interleaved A/Bs on the M16 trio): scale>=4 -
     * Sharp since iteration 2, Aggressive since iteration 4 (the foreground
     * consults the cache again for aggressive x4/x8 now that honest worker
     * fills exist; its own stores stay skipped). x2 Sharp was re-A/B'd by
     * round-2 item 2 post-quarter-res and stays OFF by default (env opt-in
     * MLVAPP_PREFETCH_INDIRECT_X2=1): the cheap quarter-res foreground
     * already fits the frame budget warm, and the worker's core-split makes
     * warm runs SLOWER (the original wash mechanism at a lower price point).
     * Aggressive x2 stays excluded unconditionally: its incompatible lanes
     * skip the main cache, so worker fills would be unreachable waste. */
    /* Round-4 item 2: mirror the foreground's lane-economics guard - when a
     * cheaper foreground lane owns the dispatch, a direct8 worker fill would
     * be stored under a never-matched envelope (wasted core-split at best). */
    const int direct8Compatible = processingCanUseDirect8BitOutput(task->processing)
        && mlv_preview_direct8_input_is_cheap(task->video, task->scaleFactor);
    if (!direct8Compatible
        && !((task->scaleFactor >= 4
              || (task->scaleFactor == 2
                  && !mlvPlaybackAggressivePreviewMode()
                  && mlv_processed8_prefetch_indirect_x2_enabled()
                  /* Round-3 item 5: the foreground x2 render now processes at
                   * quarter dims (path 11) by default; the worker's indirect
                   * render would produce the old path-5 composition and the
                   * fills would be byte-mismatched. Keep the opt-in worker
                   * coherent: it engages only when quarter processing is
                   * killed (the path-5 foreground it matches). */
                  && !mlv_quarterres_x2_processing_enabled()))
             && mlv_processed8_prefetch_indirect_enabled()
             && mlv_processed8_prefetch_indirect_state_supported(task->processing)))
    {
        processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
        processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
        processingSetPlaybackPreviewMode(previous_preview_mode);
        return;
    }

    for (uint32_t offset = task->offsetStart;
         offset <= (uint32_t)mlv_processed8_prefetch_lookahead_value();
         offset += task->offsetStep)
    {
        uint64_t targetFrame = task->baseFrame + offset;
        if (targetFrame >= getMlvFrames(task->video))
        {
            break;
        }

        uint64_t targetSignature =
            mlv_processed_frame_signature_from_state(task->stateSignature,
                                                    targetFrame);

        pthread_mutex_lock(&task->video->processed8_prefetch_mutex);
        if (task->video->processed8_prefetch_stop
            || task->generation != task->video->processed8_prefetch_generation)
        {
            pthread_mutex_unlock(&task->video->processed8_prefetch_mutex);
            break;
        }

        if (!mlv_prepare_processed_frame_8bit_cache_locked(task->video,
                                                           task->rgb_frame_size))
        {
            pthread_mutex_unlock(&task->video->processed8_prefetch_mutex);
            continue;
        }

        if (mlv_processed_frame_8bit_cache_contains_locked_with_scale(task->video,
                                                                      targetFrame,
                                                                      task->threads,
                                                                      targetSignature,
                                                                      task->generation,
                                                                      task->scaleFactor))
        {
            pthread_mutex_unlock(&task->video->processed8_prefetch_mutex);
            continue;
        }
        pthread_mutex_unlock(&task->video->processed8_prefetch_mutex);

        uint8_t * prefetchBuffer = mlv_ensure_thread_u8_buffer(task->rgb_frame_size);
        if (!prefetchBuffer)
        {
            continue;
        }

        /* The task-level preview-policy envelope (set before the gate above)
         * is active here, matching what the foreground render uses. */
        int renderOk = direct8Compatible
            ? mlv_render_processed_frame8_direct_with_processing(
                  task->video,
                  task->processing,
                  0,
                  targetFrame,
                  prefetchBuffer,
                  task->threads,
                  task->scaleFactor,
                  0)
            : mlv_render_processed_frame8_indirect_with_processing(
                  task->video,
                  task->processing,
                  targetFrame,
                  prefetchBuffer,
                  task->threads,
                  task->scaleFactor);
        const int phase4bPath = renderOk ? mlv_phase4bv2_last_path_taken() : 0;
        const int phase4bYCropRows = renderOk ? mlv_phase4bv3_last_y_crop_rows() : 0;

        int debugStored = 0;
        pthread_mutex_lock(&task->video->processed8_prefetch_mutex);
        if (task->video->processed8_prefetch_test_pause_before_store)
        {
            task->video->processed8_prefetch_test_paused_before_store = 1;
            pthread_cond_broadcast(&task->video->processed8_prefetch_cond);
            while (task->video->processed8_prefetch_test_pause_before_store
                   && !task->video->processed8_prefetch_stop
                   && task->generation == task->video->processed8_prefetch_generation)
            {
                pthread_cond_wait(&task->video->processed8_prefetch_cond,
                                  &task->video->processed8_prefetch_mutex);
            }
            task->video->processed8_prefetch_test_paused_before_store = 0;
            pthread_cond_broadcast(&task->video->processed8_prefetch_cond);
        }
        if (renderOk
            && !task->video->processed8_prefetch_stop
            && task->generation == task->video->processed8_prefetch_generation
            && !mlv_processed_frame_8bit_cache_contains_locked_with_scale(task->video,
                                                                          targetFrame,
                                                                          task->threads,
                                                                          targetSignature,
                                                                          task->generation,
                                                                          task->scaleFactor))
        {
            mlv_store_processed_frame_8bit_cache_locked_with_scale(task->video,
                                                                   targetFrame,
                                                                   task->threads,
                                                                   targetSignature,
                                                                   prefetchBuffer,
                                                                   task->rgb_frame_size,
                                                                   0,
                                                                   1,
                                                                   task->scaleFactor,
                                                                   phase4bPath,
                                                                   phase4bYCropRows);
            debugStored = 1;
        }
        pthread_mutex_unlock(&task->video->processed8_prefetch_mutex);
        if (mlv_processed8_prefetch_debug_enabled())
        {
            fprintf(stderr,
                    "[P8PF] frame=%llu renderOk=%d stored=%d direct8=%d sig=%016llx scale=%d threads=%d\n",
                    (unsigned long long)targetFrame,
                    renderOk,
                    debugStored,
                    direct8Compatible,
                    (unsigned long long)targetSignature,
                    task->scaleFactor,
                    task->threads);
        }
    }

    processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
    processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
    processingSetPlaybackPreviewMode(previous_preview_mode);
}

static void * mlv_processed8_prefetch_thread_main(void * opaque)
{
    mlvObject_t * video = (mlvObject_t *)opaque;
    processingObject_t * prefetchProcessing = initProcessingObject();

    while (1)
    {
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        while (!video->processed8_prefetch_stop
               && !video->processed8_prefetch_request_pending)
        {
            pthread_cond_wait(&video->processed8_prefetch_cond, &video->processed8_prefetch_mutex);
        }

        if (video->processed8_prefetch_stop)
        {
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            break;
        }

        /* Phase E6: rgb_frame_size depends on the requested scale; size the
         * cache buffer to match what the slot store will write. Computing
         * this from the active request keeps store and lookup byte-sized
         * the same way. */
        const int requestScale = mlv_normalize_playback_scale_factor(
            video->processed8_prefetch_request_scale);
        const int full_w = (int)getMlvWidth(video);
        const int full_h = (int)getMlvHeight(video);
        const int out_w = (requestScale > 1) ? (full_w / requestScale) : full_w;
        const int out_h = (requestScale > 1) ? (full_h / requestScale) : full_h;
        const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3u;

        if (!mlv_prepare_processed_frame_8bit_cache_locked(video, rgb_frame_size))
        {
            video->processed8_prefetch_request_pending = 0;
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            continue;
        }

        uint64_t baseFrame = video->processed8_prefetch_request_frame;
        int threads = video->processed8_prefetch_request_threads;
        int taskScale = requestScale;
        uint64_t stateSignature = video->processed8_prefetch_last_state_signature;
        uint32_t generation = video->processed8_prefetch_generation;
        if (prefetchProcessing && video->processed8_prefetch_processing)
        {
            mlv_copy_processed8_prefetch_processing_state(prefetchProcessing,
                                                          video->processed8_prefetch_processing);
        }
        video->processed8_prefetch_request_pending = 0;
        video->processed8_prefetch_worker_busy = 1;
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);

        mlv_processed8_prefetch_task_t primaryTask = {
            .video = video,
            .processing = prefetchProcessing,
            .baseFrame = baseFrame,
            .threads = threads,
            .stateSignature = stateSignature,
            .generation = generation,
            .rgb_frame_size = rgb_frame_size,
            .offsetStart = 1,
            .offsetStep = 1,
            .scaleFactor = taskScale
        };
        mlv_processed8_prefetch_execute_task(&primaryTask);

        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        video->processed8_prefetch_worker_busy = 0;
        pthread_cond_broadcast(&video->processed8_prefetch_cond);
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    }

    if (prefetchProcessing)
    {
        freeProcessingObject(prefetchProcessing);
    }

    return NULL;
}

static int mlv_start_processed8_prefetch_thread(mlvObject_t * video)
{
    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    if (video->processed8_prefetch_thread_started)
    {
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
        return 1;
    }
    video->processed8_prefetch_thread_started = 1;
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);

    if (pthread_create(&video->processed8_prefetch_thread,
                       NULL,
                       mlv_processed8_prefetch_thread_main,
                       video) != 0)
    {
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        video->processed8_prefetch_thread_started = 0;
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
        return 0;
    }

    return 1;
}

void mlvCancelPreviewPrefetch(mlvObject_t * video)
{
    if (!video)
    {
        return;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    ++video->processed8_prefetch_generation;
    mlv_reset_processed8_prefetch_locked(video);
    int processed8_busy = video->processed8_prefetch_worker_busy;
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);

    while (processed8_busy)
    {
        usleep(1000);
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        processed8_busy = video->processed8_prefetch_worker_busy;
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    }

    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    ++video->raw_uint16_prefetch_generation;
    mlv_reset_raw_uint16_prefetch_locked(video);
    int raw_busy = video->raw_uint16_prefetch_worker_busy;
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);

    while (raw_busy)
    {
        usleep(1000);
        pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
        raw_busy = video->raw_uint16_prefetch_worker_busy;
        pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    }
}

static float * mlv_ensure_float_buffer(float ** buffer, uint64_t * capacity_pixels, uint64_t required_pixels)
{
    if (!mlv_ensure_reusable_buffer((void **)buffer, capacity_pixels, required_pixels, sizeof(float)))
    {
        return NULL;
    }
    return *buffer;
}

static float * mlv_ensure_thread_float_buffer(uint64_t required_pixels)
{
    static MLV_THREAD_LOCAL float * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_pixels = 0;
    return mlv_ensure_float_buffer(&tls_buffer, &tls_capacity_pixels, required_pixels);
}

static int seek_to_next_known_block(FILE * in_file)
{
    uint64_t read_ahead_size = 128 * 1024 * 1024;
    uint8_t * ahead = malloc(read_ahead_size);

    uint64_t read = fread(ahead, 1, read_ahead_size, in_file);
    file_set_pos(in_file, -read, SEEK_CUR);
    for (uint64_t i = 0; i < read; i++)
    {
        if (memcmp(ahead + i, "VIDF", 4) == 0 ||
            memcmp(ahead + i, "AUDF", 4) == 0 ||
            memcmp(ahead + i, "NULL", 4) == 0 ||
            memcmp(ahead + i, "RTCI", 4) == 0)
        {
            DEBUG( printf("Next known block: %c%c%c%c at 0x%"PRIx64"+0x%"PRIx64" = ", ahead[i], ahead[i+1], ahead[i+2], ahead[i+3], file_get_pos(in_file), i); )
            file_set_pos(in_file, i, SEEK_CUR);
            DEBUG( printf("0x%"PRIx64"\n", file_get_pos(in_file)); )
            free(ahead);
            return 1;
        }
    }

    DEBUG( printf("Could not find any known block from 0x%"PRIx64".\n", file_get_pos(in_file)); )
    free(ahead);
    return 0;
}

/* Spanned multichunk MLV file handling */
static FILE **load_all_chunks(char *base_filename, int *entries)
{
    int seq_number = 0;
    int max_name_len = strlen(base_filename) + 16;
    char *filename = alloca(max_name_len);

    strncpy(filename, base_filename, max_name_len - 1);
    FILE **files = malloc(sizeof(FILE*));

    files[0] = fopen(filename, "rb");
    if(!files[0])
    {
        free(files);
        return NULL;
    }

    DEBUG( printf("\nFile %s opened\n", filename); )

    /* get extension and check if it is a .MLV */
    char *dot = strrchr(filename, '.');
    if(dot)
    {
        dot++;
        if(strcasecmp(dot, "mlv"))
        {
            seq_number = 100;
        }
    }

    (*entries)++;
    while(seq_number < 99)
    {
        FILE **realloc_files = realloc(files, (*entries + 1) * sizeof(FILE*));

        if(!realloc_files)
        {
            free(files);
            return NULL;
        }

        files = realloc_files;

        /* check for the next file M00, M01 etc */
        char seq_name[8];

        sprintf(seq_name, "%02d", seq_number);
        seq_number++;

        strcpy(&filename[strlen(filename) - 2], seq_name);

        /* try to open */
        files[*entries] = fopen(filename, "rb");
        if(files[*entries])
        {
            DEBUG( printf("File %s opened\n", filename); )
            (*entries)++;
        }
        else
        {
            DEBUG( printf("File %s not existing\n\n", filename); )
            break;
        }
    }

    return files;
}

static void close_all_chunks(FILE ** files, int entries)
{
    for(int i = 0; i < entries; i++)
        if(files[i]) fclose(files[i]);
    if(files) free(files);
}

static void frame_index_sort(frame_index_t *frame_index, uint32_t entries)
{
    if (!entries) return;

    uint32_t n = entries;
    do
    {
        uint32_t new_n = 1;
        for (uint32_t i = 0; i < n-1; ++i)
        {
            if (frame_index[i].frame_time > frame_index[i+1].frame_time)
            {
                frame_index_t tmp = frame_index[i+1];
                frame_index[i+1] = frame_index[i];
                frame_index[i] = tmp;
                new_n = i + 1;
            }
        }
        n = new_n;
    } while (n > 1);
}

int mlvRawFrameInputCapacity(int width, int height, int bitdepth,
                             uint32_t encoded_size,
                             size_t * packed_size,
                             size_t * allocation_size)
{
    if(!packed_size || !allocation_size || width < 3 || height < 3
       || bitdepth <= 0 || bitdepth > 16
       || (size_t)width > SIZE_MAX / (size_t)height)
        return 0;

    const size_t pixels = (size_t)width * (size_t)height;
    if(pixels == 0 || pixels > (size_t)INT_MAX
       || pixels > (SIZE_MAX - 7u) / (size_t)bitdepth
       || pixels > (SIZE_MAX - 200u) / 3u)
        return 0;

    const size_t packed_bits = pixels * (size_t)bitdepth;
    if((packed_bits & 15u) != 0) return 0;
    const size_t packed = packed_bits / 8u;
    const size_t max_encoded = pixels * 3u + 200u;
    if(max_encoded > (size_t)INT_MAX
       || (encoded_size != 0 && (size_t)encoded_size > max_encoded))
        return 0;

    const size_t selected = (size_t)encoded_size > packed
        ? (size_t)encoded_size : packed;
    if(selected > SIZE_MAX - 4u) return 0;

    *packed_size = packed;
    *allocation_size = selected + 4u;
    return 1;
}

int mlvCurvLutEntryCount(uint32_t block_size, uint32_t * lut_entries)
{
    if(!lut_entries) return 0;
    *lut_entries = 0;

    /* blockSize is file-controlled and covers the header plus the payload. */
    if(block_size < (uint32_t)sizeof(mlv_curv_hdr_t)) return 0;
    const uint32_t payload = block_size - (uint32_t)sizeof(mlv_curv_hdr_t);

    /* The payload is an array of uint16 samples and nothing else. A size that
     * is not a whole number of samples describes a block this reader cannot
     * account for, so fail the open instead of dividing the odd byte away and
     * silently ignoring it. An empty payload is likewise no curve at all. */
    if(payload == 0u || (payload % (uint32_t)sizeof(uint16_t)) != 0u) return 0;
    const uint32_t entries = payload / (uint32_t)sizeof(uint16_t);

    /* The linearisation LUT is indexed by a full 16-bit sample, so it is
     * allocated at exactly MLV_LINEARISE_LUT_ENTRIES entries. A block claiming
     * more cannot be honoured: reject it rather than truncate the curve. */
    if(entries > MLV_LINEARISE_LUT_ENTRIES) return 0;

    *lut_entries = entries;
    return 1;
}

int mlvJpeg2kBayerLayoutIsValid(size_t frame_size, int width, int height,
                                const uint32_t * offsets,
                                const uint32_t * sizes,
                                uint32_t * quarter_width,
                                uint32_t * quarter_height)
{
    if(!offsets || !sizes || !quarter_width || !quarter_height) return 0;
    *quarter_width = 0;
    *quarter_height = 0;

    /* The 2x2 bayer channel split the layout encodes is only defined for even
     * geometry; odd dimensions would silently drop a row/column instead of
     * round-tripping, so reject them rather than clamp. */
    if(width < 4 || height < 4 || (width & 1) != 0 || (height & 1) != 0)
        return 0;
    if(frame_size < MLV_JPEG2K_BAYER_HEADER_BYTES) return 0;

    const uint32_t hw = (uint32_t)width / 2u;
    const uint32_t hh = (uint32_t)height / 2u;
    const size_t quarter_pixels = (size_t)hw * (size_t)hh;
    if(quarter_pixels == 0 || quarter_pixels > SIZE_MAX / sizeof(int32_t))
        return 0;

    for(int c = 0; c < 4; c++)
    {
        /* Every channel payload must start past the fixed header and lie
         * wholly inside the frame that was actually read. The subtraction is
         * overflow-free because offsets[c] <= frame_size is checked first. */
        if(sizes[c] == 0u) return 0;
        if(offsets[c] < MLV_JPEG2K_BAYER_HEADER_BYTES) return 0;
        if((size_t)offsets[c] > frame_size) return 0;
        if((size_t)sizes[c] > frame_size - (size_t)offsets[c]) return 0;
    }

    *quarter_width = hw;
    *quarter_height = hh;
    return 1;
}

int mlvDngSequenceGeometryIsRepresentable(uint32_t width, uint32_t height,
                                          uint32_t bits_per_sample,
                                          size_t * pixel_count)
{
    if(!pixel_count || width < 3u || height < 3u
       || width > UINT16_MAX || height > UINT16_MAX
       || bits_per_sample == 0u || bits_per_sample > 16u
       || (size_t)width > SIZE_MAX / (size_t)height)
        return 0;

    const size_t pixels = (size_t)width * (size_t)height;
    if(pixels == 0 || pixels > (size_t)INT_MAX / 3u
       || pixels > SIZE_MAX / (size_t)bits_per_sample)
        return 0;
    *pixel_count = pixels;
    return 1;
}

int mlvDngCfaPatternIsSupported(uint32_t cfa_pattern)
{
    return cfa_pattern == UINT32_C(0x02010100); /* RGGB */
}

int mlvDngAsShotNeutralToWbGains(const int32_t neutral[6], uint32_t gains[3])
{
    if(!neutral || !gains) return 0;
    for(int channel = 0; channel < 3; channel++)
    {
        const int32_t numerator = neutral[channel * 2];
        const int32_t denominator = neutral[channel * 2 + 1];
        if(numerator <= 0 || denominator <= 0) return 0;
        /* MLV's custom WBAL stores the DNG AsShotNeutral ratios directly:
         * the repo's DNG writer emits wbgain_{r,g,b}/wbgain_g and the mcraw
         * importer uses the same convention.  Inverting here would swap the
         * red/blue correction on a write -> folder-open round trip. */
        const double gain = ((double)numerator / (double)denominator) * 1024.0;
        if(!isfinite(gain) || gain < 1.0 || gain > (double)UINT32_MAX) return 0;
        gains[channel] = (uint32_t)gain;
    }
    return 1;
}

static uint64_t mlvGcdU64(uint64_t a, uint64_t b)
{
    while(b != 0u)
    {
        const uint64_t next = a % b;
        a = b;
        b = next;
    }
    return a;
}

int mlvDngDefaultScaleToSampling(const int32_t scale[4],
                                 uint32_t * sampling_x,
                                 uint32_t * sampling_y)
{
    if(!scale || !sampling_x || !sampling_y
       || scale[0] <= 0 || scale[1] <= 0
       || scale[2] <= 0 || scale[3] <= 0)
        return 0;

    /* Pixel aspect is Y-scale / X-scale. The writer emits X=1 and
     * Y=(sampling_y/sampling_x), but handle any positive canonical pair. */
    uint64_t y = (uint64_t)(uint32_t)scale[2] * (uint64_t)(uint32_t)scale[1];
    uint64_t x = (uint64_t)(uint32_t)scale[3] * (uint64_t)(uint32_t)scale[0];
    const uint64_t divisor = mlvGcdU64(x, y);
    if(divisor == 0u) return 0;
    x /= divisor;
    y /= divisor;
    if(x == 0u || y == 0u || x > UINT8_MAX || y > UINT8_MAX) return 0;
    *sampling_x = (uint32_t)x;
    *sampling_y = (uint32_t)y;
    return 1;
}

int mlvDngAspectRatioIsSupported(uint32_t sampling_x, uint32_t sampling_y)
{
    if(sampling_x == 0u || sampling_y == 0u) return 0;
    static const uint32_t horizontal[][2] = {
        {1u,1u}, {5u,4u}, {4u,3u}, {3u,2u},
        {5u,3u}, {7u,4u}, {9u,5u}, {2u,1u}
    };
    static const uint32_t vertical[][2] = {
        {1u,1u}, {5u,3u}, {3u,1u}, {1u,3u}
    };
    for(size_t h = 0; h < sizeof(horizontal) / sizeof(horizontal[0]); ++h)
    {
        for(size_t v = 0; v < sizeof(vertical) / sizeof(vertical[0]); ++v)
        {
            /* sampling_y/sampling_x == vertical/horizontal */
            const uint64_t left = (uint64_t)sampling_y * vertical[v][1]
                                * horizontal[h][0];
            const uint64_t right = (uint64_t)sampling_x * vertical[v][0]
                                 * horizontal[h][1];
            if(left == right) return 1;
        }
    }
    return 0;
}

static uint64_t mlvAbsI64ToU64(int64_t value)
{
    return value < 0 ? (uint64_t)(-(value + 1)) + 1u : (uint64_t)value;
}

int mlvDngBaselineExposureToBias(const int32_t baseline[2],
                                 const int32_t offset[2],
                                 int32_t bias[2])
{
    if(!baseline || !offset || !bias
       || baseline[1] == 0 || offset[1] == 0) return 0;
    int64_t bnum = baseline[0], bden = baseline[1];
    int64_t onum = offset[0], oden = offset[1];
    if(bden < 0) { bnum = -bnum; bden = -bden; }
    if(oden < 0) { onum = -onum; oden = -oden; }
    const uint64_t denominator_gcd = mlvGcdU64((uint64_t)bden, (uint64_t)oden);
    const int64_t bscale = oden / (int64_t)denominator_gcd;
    const int64_t oscale = bden / (int64_t)denominator_gcd;
    const uint64_t babs = mlvAbsI64ToU64(bnum);
    const uint64_t oabs = mlvAbsI64ToU64(onum);
    if((babs != 0u && (uint64_t)bscale > (uint64_t)INT64_MAX / babs)
       || (oabs != 0u && (uint64_t)oscale > (uint64_t)INT64_MAX / oabs))
        return 0;
    const int64_t bterm = bnum * bscale;
    const int64_t oterm = onum * oscale;
    if((oterm > 0 && bterm > INT64_MAX - oterm)
       || (oterm < 0 && bterm < INT64_MIN - oterm)) return 0;
    const int64_t numerator = bterm + oterm;
    if((uint64_t)bden > (uint64_t)INT64_MAX / (uint64_t)bscale) return 0;
    const int64_t denominator = bden * bscale;
    if(denominator <= 0) return 0;
    const uint64_t divisor = mlvGcdU64(mlvAbsI64ToU64(numerator),
                                      (uint64_t)denominator);
    if(divisor == 0u) return 0;
    const int64_t normalized_num = numerator / (int64_t)divisor;
    const int64_t normalized_den = denominator / (int64_t)divisor;
    if(normalized_num < INT32_MIN || normalized_num > INT32_MAX
       || normalized_den <= 0 || normalized_den > INT32_MAX) return 0;
    bias[0] = (int32_t)normalized_num;
    bias[1] = (int32_t)normalized_den;
    return 1;
}

/* Unpack or decompress original raw data */
static int getMlvRawFrameUint16Direct(mlvObject_t * video, uint64_t frameIndex, uint16_t * unpackedFrame)
{
    int bitdepth = video->RAWI.raw_info.bits_per_pixel;
    int width = video->RAWI.xRes;
    int height = video->RAWI.yRes;
    size_t packed_frame_size = 0;
    size_t raw_frame_capacity = 0;

    /* CinemaDNG folder source: there is no MLV/mcraw file[chunk] container.
     * Each frame is its own .dng file. Decode the requested frame's bayer
     * payload directly into unpackedFrame (uncompressed bit-unpack OR LJ92,
     * picked from the per-frame TIFF Compression tag) and return. The output
     * is the same row-major uint16 bayer frame the MLV path produces, so the
     * entire downstream llrawproc/AMaZE/processing/display pipeline is reused
     * unchanged. */
    if (isDngFolderLoaded(video))
    {
        const double disk_read_start = mlv_stage_timing_now();
        int ret = dng_sequence_get_bayer16((const dng_sequence_t *)video->dng_sequence,
                                            (uint32_t)frameIndex, unpackedFrame);
        g_mlv_last_raw_uint16_disk_read_ms = (mlv_stage_timing_now() - disk_read_start) * 1000.0;
        g_mlv_last_raw_uint16_decompress_ms = 0.0;
        g_mlv_last_raw_uint16_unpack_ms = 0.0;
        g_mlv_last_raw_uint16_copy_ms = 0.0;
        if (ret != 0)
        {
            DEBUG( printf("DNG folder decode error for frame %llu\n", (unsigned long long)frameIndex); )
            return 1;
        }

        /* Mirror the optional S0 pipeline-capture hook from the MLV path. */
        if (mlv_pipeline_capture_should_capture_frame(frameIndex))
        {
            const int cw = (int)getMlvWidth(video);
            const int ch = (int)getMlvHeight(video);
            mlv_pipeline_capture_meta_t meta;
            memset(&meta, 0, sizeof meta);
            meta.stage = MLV_PIPELINE_STAGE_S0_RAW_UINT16;
            meta.format = MLV_PIPELINE_FORMAT_UINT16_MONO;
            meta.format_label = "uint16_bayer_post_unpack";
            meta.width = cw;
            meta.height = ch;
            meta.bytes_per_line = cw * (int)sizeof(uint16_t);
            meta.bytes_per_pixel = (int)sizeof(uint16_t);
            meta.channels = 1;
            meta.bit_depth = 16;
            meta.dual_iso_mode = "n/a";
            meta.debayer_mode = "n/a";
            meta.scaler = "none";
            meta.path_label = NULL;
            mlv_pipeline_capture(frameIndex, unpackedFrame, &meta);
        }
        return 0;
    }

    if(!mlvRawFrameInputCapacity(width, height, bitdepth, 0,
                                 &packed_frame_size, &raw_frame_capacity))
        return 1;
    const int pixels_count = width * height;

    int chunk = video->video_index[frameIndex].chunk_num;
    uint32_t frame_size = video->video_index[frameIndex].frame_size;
    uint64_t frame_offset = video->video_index[frameIndex].frame_offset;
    uint64_t frame_header_offset = video->video_index[frameIndex].block_offset;

    /* Every encoded class must size raw_frame from the encoded frame size, not
     * from the packed RAW size: the codec branches read frame_size bytes. */
    const int compressed_input = (isMcrawLoaded(video))
        || (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92)
        || (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_JPEG2K);
    if(compressed_input && !(isMcrawLoaded(video))
       && !mlvRawFrameInputCapacity(width, height, bitdepth, frame_size,
                                    &packed_frame_size, &raw_frame_capacity))
        return 1;
    /* Additional four bytes preserve the legacy bit-unpack look-ahead while
     * every read is bounded by the retained allocation capacity. */
    uint8_t * raw_frame = (uint8_t *)malloc(raw_frame_capacity);
    if(!raw_frame) return 1;

    g_mlv_last_raw_uint16_disk_read_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_prepare_ms = 0.0;
    g_mlv_last_raw_uint16_decompress_execute_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_split_active = 0;
    g_mlv_last_raw_uint16_lj92_pred6_split_requested = 0;
    g_mlv_last_raw_uint16_lj92_generic_split_active = 0;
    g_mlv_last_raw_uint16_lj92_generic_split_requested = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_active = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active = 0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible = 0;
    g_mlv_last_raw_uint16_lj92_scan_component_count = 0;
    g_mlv_last_raw_uint16_lj92_write_length = 0;
    g_mlv_last_raw_uint16_lj92_expected_write_length = 0;
    g_mlv_last_raw_uint16_lj92_skip_length = 0;
    g_mlv_last_raw_uint16_lj92_linearize_active = 0;
    g_mlv_last_raw_uint16_lj92_component_count = 0;
    g_mlv_last_raw_uint16_lj92_predictor = -1;
    g_mlv_last_raw_uint16_lj92_pred6_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred6_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_generic_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms = 0.0;
    g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms = 0.0;
    g_mlv_last_raw_uint16_unpack_ms = 0.0;
    g_mlv_last_raw_uint16_copy_ms = 0.0;

    FILE * file = video->file[chunk];

    /* Move to start of frame in file and read the RAW data */
    pthread_mutex_lock(video->main_file_mutex + chunk);

    file_set_pos(file, frame_header_offset, SEEK_SET);

    if (isMcrawLoaded(video))
    {
        mr_item_t item = {};
        const double disk_read_start = mlv_stage_timing_now();

        if (fread(&item, sizeof(mr_item_t), 1, file) != 1)
        {
            DEBUG( printf("Frame header read error\n"); )
            free(raw_frame);
            pthread_mutex_unlock(video->main_file_mutex + chunk);
            return 1;
        }

        frame_size = item.size;

        size_t item_packed_size = 0;
        size_t item_capacity = 0;
        if(!mlvRawFrameInputCapacity(width, height, bitdepth, frame_size,
                                     &item_packed_size, &item_capacity))
        {
            free(raw_frame);
            pthread_mutex_unlock(video->main_file_mutex + chunk);
            return 1;
        }
        if(item_capacity > raw_frame_capacity)
        {
            uint8_t * replacement = (uint8_t *)realloc(raw_frame, item_capacity);
            if(!replacement)
            {
                free(raw_frame);
                pthread_mutex_unlock(video->main_file_mutex + chunk);
                return 1;
            }
            raw_frame = replacement;
            raw_frame_capacity = item_capacity;
        }

        if ((size_t)frame_size > raw_frame_capacity - 4u
            || fread(raw_frame, frame_size, 1, file) != 1)
        {
            DEBUG( printf("Frame data read error\n"); )
            free(raw_frame);
            pthread_mutex_unlock(video->main_file_mutex + chunk);
            return 1;
        }

        pthread_mutex_unlock(video->main_file_mutex + chunk);
        g_mlv_last_raw_uint16_disk_read_ms = (mlv_stage_timing_now() - disk_read_start) * 1000.0;

        const double decompress_start = mlv_stage_timing_now();
        int64_t ret = mr_decode_video_frame((uint8_t*)unpackedFrame, raw_frame, frame_size, width, height, video->compression_type);
        g_mlv_last_raw_uint16_decompress_execute_ms = (mlv_stage_timing_now() - decompress_start) * 1000.0;
        g_mlv_last_raw_uint16_decompress_ms = g_mlv_last_raw_uint16_decompress_execute_ms;

        if (ret <= 0)
        {
            DEBUG( printf("mcraw decoder: Failed with error code (%d)\n", ret); )
            free(raw_frame);
            return 1;
        }

        if (video->RAWI.raw_info.cfa_pattern == 0x01000201)   // gbrg
        {
            const double copy_start = mlv_stage_timing_now();
            // gb  ->  rg
            // rg      gb

            // discard first row
            memmove(unpackedFrame,
                    &unpackedFrame[width],
                    (size_t)width * (size_t)(height - 1) * sizeof(*unpackedFrame));

            // copy row n-2 to row n
            memcpy(&unpackedFrame[width * (height - 1)], &unpackedFrame[width * (height - 3)], width * 2);
            g_mlv_last_raw_uint16_copy_ms = (mlv_stage_timing_now() - copy_start) * 1000.0;
        }
        else if (video->RAWI.raw_info.cfa_pattern == 0x00010102)   // bggr
        {
            const double copy_start = mlv_stage_timing_now();
            // bg  ->  rg
            // gr      gb

            // !!untested!!

            // discard first row, discard first col
            memmove(unpackedFrame, &unpackedFrame[width + 1],
                    (size_t)width * (size_t)(height - 1) * sizeof(*unpackedFrame)
                        - sizeof(*unpackedFrame));

            // copy row n-2 to row n
            memcpy(&unpackedFrame[width * (height - 1)],
                   &unpackedFrame[width * (height - 3)],
                   (size_t)width * sizeof(*unpackedFrame));

            // copy col n-2 to col n
            for (int i = 0; i < height; i++)
            {
                int pos = ((i + 1) * width) - 1;
                unpackedFrame[pos] = unpackedFrame[pos - 2];
            }
            g_mlv_last_raw_uint16_copy_ms = (mlv_stage_timing_now() - copy_start) * 1000.0;
        }
        else if (video->RAWI.raw_info.cfa_pattern == 0x01020001)   // grbg
        {
            const double copy_start = mlv_stage_timing_now();
            // gr  ->  rg
            // bg      gb

            // !!untested!!

            // discard first col
            memmove(unpackedFrame, &unpackedFrame[1],
                    (size_t)pixels_count * sizeof(*unpackedFrame)
                        - sizeof(*unpackedFrame));

            // copy col n-2 to col n
            for (int i = 0; i < height; i++)
            {
                int pos = ((i + 1) * width) - 1;
                unpackedFrame[pos] = unpackedFrame[pos - 2];
            }
            g_mlv_last_raw_uint16_copy_ms = (mlv_stage_timing_now() - copy_start) * 1000.0;
        }
    }
    else
    {
        const double disk_read_start = mlv_stage_timing_now();
        if (fread(&video->VIDF, sizeof(mlv_vidf_hdr_t), 1, file) != 1)
        {
            DEBUG( printf("Frame header read error\n"); )
            free(raw_frame);
            pthread_mutex_unlock(video->main_file_mutex + chunk);
            return 1;
        }

        file_set_pos(file, frame_offset, SEEK_SET);

        if (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92)
        {
            if((size_t)frame_size > raw_frame_capacity - 4u
               || fread(raw_frame, frame_size, 1, file) != 1)
            {
                DEBUG( printf("Frame data read error lj92\n"); )
                free(raw_frame);
                pthread_mutex_unlock(video->main_file_mutex + chunk);
                return 1;
            }

            pthread_mutex_unlock(video->main_file_mutex + chunk);
            g_mlv_last_raw_uint16_disk_read_ms = (mlv_stage_timing_now() - disk_read_start) * 1000.0;

            const int expected_width = width;
            const int expected_height = height;
            const int expected_bitdepth = bitdepth;
            int components = 1;
            lj92 decoder_object;
            const double decompress_start = mlv_stage_timing_now();
            const double decompress_prepare_start = mlv_stage_timing_now();
            int ret = lj92_open(&decoder_object, raw_frame, frame_size, &width, &height, &bitdepth, &components);
            g_mlv_last_raw_uint16_decompress_prepare_ms =
                (mlv_stage_timing_now() - decompress_prepare_start) * 1000.0;
            if(ret != LJ92_ERROR_NONE)
            {
                DEBUG( printf("LJ92 decoder: Failed with error code (%d)\n", ret); )
                free(raw_frame);
                return 1;
            }
            else
            {
                if(width <= 0 || height <= 0 || components <= 0
                   || (components != 1 && components != 2)
                   || bitdepth != expected_bitdepth
                   || (size_t)width > SIZE_MAX / (size_t)height
                   || (size_t)width * (size_t)height > SIZE_MAX / (size_t)components
                   || (size_t)width * (size_t)height * (size_t)components
                        != (size_t)expected_width * (size_t)expected_height)
                {
                    lj92_close(decoder_object);
                    free(raw_frame);
                    return 1;
                }
                const double decompress_execute_start = mlv_stage_timing_now();
                ret = lj92_decode(decoder_object, unpackedFrame, pixels_count, 0, NULL, 0);
                g_mlv_last_raw_uint16_decompress_execute_ms =
                    (mlv_stage_timing_now() - decompress_execute_start) * 1000.0;
                g_mlv_last_raw_uint16_lj92_pred6_split_active =
                    lj92_get_last_pred6_split_active();
                g_mlv_last_raw_uint16_lj92_pred6_split_requested =
                    lj92_get_last_pred6_split_requested();
                g_mlv_last_raw_uint16_lj92_generic_split_active =
                    lj92_get_last_generic_split_active();
                g_mlv_last_raw_uint16_lj92_generic_split_requested =
                    lj92_get_last_generic_split_requested();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_active =
                    lj92_get_last_pred1_fast_path_active();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested =
                    lj92_get_last_pred1_fast_path_measurement_requested();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active =
                    lj92_get_last_pred1_fast_path_measurement_active();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible =
                    lj92_get_last_pred1_fast_path_eligible();
                g_mlv_last_raw_uint16_lj92_scan_component_count =
                    lj92_get_last_scan_component_count();
                g_mlv_last_raw_uint16_lj92_write_length =
                    lj92_get_last_write_length();
                g_mlv_last_raw_uint16_lj92_expected_write_length =
                    lj92_get_last_expected_write_length();
                g_mlv_last_raw_uint16_lj92_skip_length =
                    lj92_get_last_skip_length();
                g_mlv_last_raw_uint16_lj92_linearize_active =
                    lj92_get_last_linearize_active();
                g_mlv_last_raw_uint16_lj92_component_count =
                    lj92_get_last_component_count();
                g_mlv_last_raw_uint16_lj92_predictor =
                    lj92_get_last_predictor();
                g_mlv_last_raw_uint16_lj92_pred6_total_ms =
                    lj92_get_last_pred6_total_ms();
                g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms =
                    lj92_get_last_pred6_bitstream_ms();
                g_mlv_last_raw_uint16_lj92_pred6_predictor_ms =
                    lj92_get_last_pred6_predictor_ms();
                g_mlv_last_raw_uint16_lj92_generic_total_ms =
                    lj92_get_last_generic_total_ms();
                g_mlv_last_raw_uint16_lj92_generic_bitstream_ms =
                    lj92_get_last_generic_bitstream_ms();
                g_mlv_last_raw_uint16_lj92_generic_predictor_ms =
                    lj92_get_last_generic_predictor_ms();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms =
                    lj92_get_last_pred1_fast_path_total_ms();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms =
                    lj92_get_last_pred1_fast_path_bitstream_ms();
                g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms =
                    lj92_get_last_pred1_fast_path_predictor_ms();
                if(ret != LJ92_ERROR_NONE)
                {
                    DEBUG( printf("LJ92 decoder: Failed with error code (%d)\n", ret); )
                    free(raw_frame);
                    return 1;
                }
            }
            lj92_close(decoder_object);
            g_mlv_last_raw_uint16_decompress_ms = (mlv_stage_timing_now() - decompress_start) * 1000.0;
        }
        else if (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_JPEG2K)
        {
#ifdef ENABLE_JPEG2K
            /* frame_size is file-controlled: never read past the capacity that
             * mlvRawFrameInputCapacity admitted for this encoded frame. */
            if((size_t)frame_size > raw_frame_capacity - 4u
               || (size_t)frame_size < MLV_JPEG2K_BAYER_HEADER_BYTES
               || fread(raw_frame, frame_size, 1, file) != 1)
            {
                DEBUG( printf("Frame data read error jpeg2k\n"); )
                free(raw_frame);
                pthread_mutex_unlock(video->main_file_mutex + chunk);
                return 1;
            }

            pthread_mutex_unlock(video->main_file_mutex + chunk);

            /* Parse bayer JPEG2K header: version + 8 u32s (offset/size for 4 channels) */
            uint32_t hdr[9];
            memcpy(hdr, raw_frame, sizeof(hdr));
            if(hdr[0] != 1)
            {
                DEBUG( printf("JPEG2K decoder: unsupported version of JPEG2K MLV layout.\n"); )
                free(raw_frame);
                return 1;
            }
            uint32_t sizes[4]  = { hdr[2], hdr[4], hdr[6], hdr[8] };
            uint32_t offsets[4] = { hdr[1], hdr[3], hdr[5], hdr[7] };

            uint32_t hw = 0;
            uint32_t hh = 0;
            if(!mlvJpeg2kBayerLayoutIsValid((size_t)frame_size, width, height,
                                            offsets, sizes, &hw, &hh))
            {
                DEBUG( printf("JPEG2K decoder: malformed channel table or geometry.\n"); )
                free(raw_frame);
                return 1;
            }
            size_t quarter_pixels = (size_t)hw * hh;

            /* Allocate 4 separate quarter buffers */
            int32_t *quarter_bufs[4] = { NULL, NULL, NULL, NULL };
            int alloc_ok = 1;
            for(int c = 0; c < 4; c++)
            {
                quarter_bufs[c] = (int32_t *)malloc(quarter_pixels * sizeof(int32_t));
                if(!quarter_bufs[c])
                {
                    DEBUG( printf("JPEG2K decoder: memory allocation failed\n"); )
                    alloc_ok = 0;
                    break;
                }
            }
            if(!alloc_ok)
            {
                for(int c = 0; c < 4; c++) free(quarter_bufs[c]);
                free(raw_frame);
                return 1;
            }

            /* Decode 4 channels in parallel into separate buffers */
            int decode_errors[4] = { 0, 0, 0, 0 };
            #pragma omp parallel for num_threads(4)
            for(int c = 0; c < 4; c++)
            {
                uint8_t *encoded = ((uint8_t *)raw_frame) + offsets[c];
                uint32_t enc_size = sizes[c];

                void *decoder = ojph_decoder_new();
                if(!decoder)
                {
                    DEBUG( printf("JPEG2K decoder: failed to create decoder\n"); )
                    decode_errors[c] = 1;
                    continue;
                }

                uint32_t dw = 0, dh = 0, nc = 0, bd = 0;
                int is_signed = 0;
                int ret = ojph_decoder_probe(decoder, encoded, enc_size,
                                             &dw, &dh, &nc, &bd, &is_signed);
                if(ret != 0)
                {
                    DEBUG( printf("JPEG2K decoder: probe failed channel %d (error %d)\n", c, ret); )
                    ojph_decoder_free(decoder);
                    decode_errors[c] = 1;
                    continue;
                }

                /* The probed geometry is attacker-controlled. It must match the
                 * quarter frame derived from RAWI exactly - decoding a larger
                 * image into quarter_bufs[c] would write out of bounds, and a
                 * smaller one would leave the scatter reading uninitialised
                 * samples. Reject either, never clamp. */
                if(dw != hw || dh != hh || nc != 1u)
                {
                    DEBUG( printf("JPEG2K decoder: channel %d geometry mismatch\n", c); )
                    ojph_decoder_free(decoder);
                    decode_errors[c] = 1;
                    continue;
                }

                size_t pix_count = quarter_pixels;
                size_t decoded = ojph_decoder_decode_into(decoder, encoded, enc_size,
                                                           quarter_bufs[c], quarter_pixels,
                                                           &dw, &dh, &nc);
                if(decoded == 0 || decoded != pix_count
                   || dw != hw || dh != hh || nc != 1u)
                {
                    DEBUG( printf("JPEG2K decoder: decode failed channel %d\n", c); )
                    ojph_decoder_free(decoder);
                    decode_errors[c] = 1;
                    continue;
                }

                ojph_decoder_free(decoder);
            }

            /* Check for decode errors */
            for(int c = 0; c < 4; c++)
            {
                if(decode_errors[c])
                {
                    for(int c2 = 0; c2 < 4; c2++) free(quarter_bufs[c2]);
                    free(raw_frame);
                    return 1;
                }
            }

            /* Scatter 4 quarter buffers into full bayer frame
             * Channel order: 0=x0y0, 1=x1y0, 2=x0y1, 3=x1y1 */
            for(int c = 0; c < 4; c++)
            {
                uint32_t x_off = (c == 1 || c == 3) ? 1 : 0;
                uint32_t y_off = (c == 2 || c == 3) ? 1 : 0;

                for(uint32_t y = 0; y < hh; y++)
                {
                    for(uint32_t x = 0; x < hw; x++)
                    {
                        int32_t val = quarter_bufs[c][y * hw + x];
                        if(val < 0) val = 0;
                        if(val > 65535) val = 65535;
                        unpackedFrame[((y * 2 + y_off) * width) + (x * 2 + x_off)] = (uint16_t)val;
                    }
                }
            }

            for(int c = 0; c < 4; c++) free(quarter_bufs[c]);
#else
            DEBUG( printf("JPEG2K codec is not enabled at build\n"); )
            free(raw_frame);
            pthread_mutex_unlock(video->main_file_mutex + chunk);
            return 1;
#endif
        }
        else /* If not compressed just unpack to 16bit */
        {
            if(packed_frame_size > raw_frame_capacity - 4u
               || fread(raw_frame, packed_frame_size, 1, file) != 1)
            {
                DEBUG( printf("Frame data read error none\n"); )
                free(raw_frame);
                pthread_mutex_unlock(video->main_file_mutex + chunk);
                return 1;
            }

            pthread_mutex_unlock(video->main_file_mutex + chunk);
            g_mlv_last_raw_uint16_disk_read_ms = (mlv_stage_timing_now() - disk_read_start) * 1000.0;

            const double unpack_start = mlv_stage_timing_now();
            dng_unpack_image_bits(unpackedFrame, (uint16_t *)raw_frame,
                                  width, height, (uint32_t)bitdepth);
            g_mlv_last_raw_uint16_unpack_ms = (mlv_stage_timing_now() - unpack_start) * 1000.0;
        }
    }

    if (video->linearise_lut)
    {
        #pragma omp parallel for
        for (int i = 0; i < pixels_count; ++i)
        {
            unpackedFrame[i] = video->linearise_lut[unpackedFrame[i]];
        }
    }

    free(raw_frame);

    /* S0_raw_uint16 capture: post LJ92 decode + bit-unpack, pre llrawproc.
     * Inert when MLVAPP_PIPELINE_CAPTURE_DIR is unset. */
    if (mlv_pipeline_capture_should_capture_frame(frameIndex))
    {
        const int width = (int)getMlvWidth(video);
        const int height = (int)getMlvHeight(video);
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = MLV_PIPELINE_STAGE_S0_RAW_UINT16;
        meta.format = MLV_PIPELINE_FORMAT_UINT16_MONO;
        meta.format_label = "uint16_bayer_post_unpack";
        meta.width = width;
        meta.height = height;
        meta.bytes_per_line = width * (int)sizeof(uint16_t);
        meta.bytes_per_pixel = (int)sizeof(uint16_t);
        meta.channels = 1;
        meta.bit_depth = 16;
        meta.dual_iso_mode = "n/a";
        meta.debayer_mode = "n/a";
        meta.scaler = "none";
        meta.path_label = NULL;
        mlv_pipeline_capture(frameIndex, unpackedFrame, &meta);
    }

    return 0;
}

int getMlvRawFrameUint16(mlvObject_t * video, uint64_t frameIndex, uint16_t * unpackedFrame)
{
    const int compressedRaw = isMcrawLoaded(video)
        || (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92);
    const int prefetchEnabled = compressedRaw && mlv_raw_uint16_prefetch_allowed_for_request(video);

    g_mlv_last_raw_uint16_prefetch_hit = 0;

    if (prefetchEnabled && mlv_raw_uint16_prefetch_try_copy(video, frameIndex, unpackedFrame))
    {
        g_mlv_last_raw_uint16_disk_read_ms = 0.0;
        g_mlv_last_raw_uint16_decompress_ms = 0.0;
        g_mlv_last_raw_uint16_decompress_prepare_ms = 0.0;
        g_mlv_last_raw_uint16_decompress_execute_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred6_split_active = 0;
        g_mlv_last_raw_uint16_lj92_pred6_split_requested = 0;
        g_mlv_last_raw_uint16_lj92_generic_split_active = 0;
        g_mlv_last_raw_uint16_lj92_generic_split_requested = 0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_active = 0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested = 0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active = 0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible = 0;
        g_mlv_last_raw_uint16_lj92_scan_component_count = 0;
        g_mlv_last_raw_uint16_lj92_write_length = 0;
        g_mlv_last_raw_uint16_lj92_expected_write_length = 0;
        g_mlv_last_raw_uint16_lj92_skip_length = 0;
        g_mlv_last_raw_uint16_lj92_linearize_active = 0;
        g_mlv_last_raw_uint16_lj92_component_count = 0;
        g_mlv_last_raw_uint16_lj92_predictor = -1;
        g_mlv_last_raw_uint16_lj92_pred6_total_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred6_predictor_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_generic_total_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_generic_bitstream_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_generic_predictor_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms = 0.0;
        g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms = 0.0;
        g_mlv_last_raw_uint16_unpack_ms = 0.0;
        g_mlv_last_raw_uint16_copy_ms = 0.0;
        g_mlv_last_raw_uint16_prefetch_hit = 1;
        mlv_raw_uint16_prefetch_note_request(video, frameIndex);
        return 0;
    }

    int result = getMlvRawFrameUint16Direct(video, frameIndex, unpackedFrame);
    if (result == 0 && prefetchEnabled)
    {
        mlv_raw_uint16_prefetch_store_frame(video, frameIndex, unpackedFrame);
        mlv_raw_uint16_prefetch_note_request(video, frameIndex);
    }

    return result;
}

static int getMlvRawFrameProcessedUint16DirectImpl(mlvObject_t * video,
                                                   uint64_t frameIndex,
                                                   uint16_t * outputFrame,
                                                   int * bit_shift,
                                                   int chroma_smooth_override_enabled,
                                                   int chroma_smooth_method)
{
    int pixels_count = video->RAWI.xRes * video->RAWI.yRes;
    size_t output_frame_size = (size_t)pixels_count * sizeof(uint16_t);

    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_raw_float_convert_ms = 0.0;

    const double unpack_start = mlv_stage_timing_now();
    if(getMlvRawFrameUint16Direct(video, frameIndex, outputFrame))
    {
        memset(outputFrame, 0, output_frame_size);
        mlv_stage_timing_note("raw_uint16", frameIndex, unpack_start);
        return 1;
    }
    const double raw_uint16_ms = (mlv_stage_timing_now() - unpack_start) * 1000.0;
    mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, raw_uint16_ms);
    g_mlv_last_raw_uint16_ms = raw_uint16_ms;
    mlv_stage_timing_note_elapsed("raw_uint16_disk_read", frameIndex, g_mlv_last_raw_uint16_disk_read_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress", frameIndex, g_mlv_last_raw_uint16_decompress_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress_prepare", frameIndex, g_mlv_last_raw_uint16_decompress_prepare_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress_execute", frameIndex, g_mlv_last_raw_uint16_decompress_execute_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_unpack", frameIndex, g_mlv_last_raw_uint16_unpack_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_copy", frameIndex, g_mlv_last_raw_uint16_copy_ms);

    llrawprocWorkerState_t analysis_worker;
    llrpInitWorkerState(&analysis_worker);

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    if (chroma_smooth_override_enabled)
    {
        applyLLRawProcObjectWorkerIsolatedAnalysisWithChromaSmooth(
            video,
            outputFrame,
            output_frame_size,
            &analysis_worker,
            0,
            chroma_smooth_method);
    }
    else
    {
        applyLLRawProcObjectWorkerIsolatedAnalysis(video,
                                                   outputFrame,
                                                   output_frame_size,
                                                   &analysis_worker,
                                                   0);
    }
    llrpFreeWorkerState(&analysis_worker);
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    if (bit_shift)
    {
        *bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    }

    return 0;
}

int getMlvRawFrameProcessedUint16Direct(mlvObject_t * video,
                                        uint64_t frameIndex,
                                        uint16_t * outputFrame,
                                        int * bit_shift)
{
    return getMlvRawFrameProcessedUint16DirectImpl(video,
                                                   frameIndex,
                                                   outputFrame,
                                                   bit_shift,
                                                   0,
                                                   CS_OFF);
}

int getMlvRawFrameProcessedUint16DirectWithChromaSmooth(mlvObject_t * video,
                                                        uint64_t frameIndex,
                                                        uint16_t * outputFrame,
                                                        int * bit_shift,
                                                        int chroma_smooth_method)
{
    return getMlvRawFrameProcessedUint16DirectImpl(video,
                                                   frameIndex,
                                                   outputFrame,
                                                   bit_shift,
                                                    1,
                                                    chroma_smooth_method);
}

int get_mlv_raw_frame_debayered_isolated_analysis_scaled(mlvObject_t * video,
                                                         uint64_t frameIndex,
                                                         uint16_t * outputFrame,
                                                         int scaleFactor,
                                                         int threads,
                                                         int * outWidth,
                                                         int * outHeight,
                                                         int * outScaleFactor)
{
    if (!video || !outputFrame)
    {
        return 0;
    }

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int normalized_scale = mlv_effective_playback_scale_factor(video, scaleFactor);
    if (full_w <= 0 || full_h <= 0 || normalized_scale <= 1)
    {
        return 0;
    }

    const int out_w = full_w / normalized_scale;
    const int out_h = full_h / normalized_scale;
    if (out_w <= 0 || out_h <= 0)
    {
        return 0;
    }

    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t out_words = (uint64_t)out_w * (uint64_t)out_h * 3u;
    uint16_t * raw_frame = mlv_ensure_thread_u16_buffer(full_pixels);
    if (!raw_frame)
    {
        memset(outputFrame, 0, (size_t)out_words * sizeof(uint16_t));
        return 0;
    }

    int bit_shift = 0;
    if (getMlvRawFrameProcessedUint16Direct(video, frameIndex, raw_frame, &bit_shift))
    {
        memset(outputFrame, 0, (size_t)out_words * sizeof(uint16_t));
        return 0;
    }

    const int downsample_threads = threads > 0 ? threads : 1;
    const double debayer_start = mlv_stage_timing_now();
    if (normalized_scale == 8)
    {
        pl_downsample_bayer_to_rgb_8x(raw_frame, full_w, full_h,
                                      outputFrame, bit_shift, downsample_threads);
    }
    else if (normalized_scale == 4)
    {
        pl_downsample_bayer_to_rgb_4x(raw_frame, full_w, full_h,
                                      outputFrame, bit_shift, downsample_threads);
    }
    else
    {
        pl_downsample_bayer_to_rgb_2x(raw_frame, full_w, full_h,
                                      outputFrame, bit_shift, downsample_threads);
    }
    g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);

    if (outWidth)
    {
        *outWidth = out_w;
    }
    if (outHeight)
    {
        *outHeight = out_h;
    }
    if (outScaleFactor)
    {
        *outScaleFactor = normalized_scale;
    }
    return 1;
}

int getMlvRawFrameProcessedUint16(mlvObject_t * video,
                                  uint64_t frameIndex,
                                  uint16_t * outputFrame,
                                  int * bit_shift)
{
    int pixels_count = video->RAWI.xRes * video->RAWI.yRes;
    size_t output_frame_size = (size_t)pixels_count * sizeof(uint16_t);

    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_raw_float_convert_ms = 0.0;

    const double unpack_start = mlv_stage_timing_now();
    if(getMlvRawFrameUint16(video, frameIndex, outputFrame))
    {
        memset(outputFrame, 0, output_frame_size);
        mlv_stage_timing_note("raw_uint16", frameIndex, unpack_start);
        return 1;
    }
    const double raw_uint16_ms = (mlv_stage_timing_now() - unpack_start) * 1000.0;
    mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, raw_uint16_ms);
    g_mlv_last_raw_uint16_ms = raw_uint16_ms;
    mlv_stage_timing_note_elapsed("raw_uint16_disk_read", frameIndex, g_mlv_last_raw_uint16_disk_read_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress", frameIndex, g_mlv_last_raw_uint16_decompress_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress_prepare", frameIndex, g_mlv_last_raw_uint16_decompress_prepare_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_decompress_execute", frameIndex, g_mlv_last_raw_uint16_decompress_execute_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_unpack", frameIndex, g_mlv_last_raw_uint16_unpack_ms);
    mlv_stage_timing_note_elapsed("raw_uint16_copy", frameIndex, g_mlv_last_raw_uint16_copy_ms);

    const double llraw_start = mlv_stage_timing_now();
    /* Make the frame index available to S1/S2 capture hooks inside
     * applyLLRawProcObject (which doesn't otherwise carry one). */
    mlv_pipeline_capture_set_current_frame(frameIndex);
    applyLLRawProcObject(video, outputFrame, output_frame_size);
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    if (bit_shift)
    {
        *bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    }

    return 0;
}

/* Unpacks the bits of a frame to get a bayer B&W image (without black level correction)
 * Needs memory to return to, sized: sizeof(float) * getMlvHeight(urvid) * getMlvWidth(urvid)
 * Output image's pixels will be in range 0-65535 as if it is 16 bit integers */
void getMlvRawFrameFloat(mlvObject_t * video, uint64_t frameIndex, float * outputFrame)
{
    const double total_start = mlv_stage_timing_now();
    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_raw_float_convert_ms = 0.0;
    int pixels_count = video->RAWI.xRes * video->RAWI.yRes;
    uint16_t * unpacked_frame = mlv_ensure_thread_u16_buffer((uint64_t)pixels_count);
    if (!unpacked_frame)
    {
        memset(outputFrame, 0, pixels_count * sizeof(float));
        mlv_stage_timing_note("raw_float_total", frameIndex, total_start);
        return;
    }

    int shift_val = 0;
    if(getMlvRawFrameProcessedUint16(video, frameIndex, unpacked_frame, &shift_val))
    {
        memset(outputFrame, 0, pixels_count * sizeof(float));
        mlv_stage_timing_note("raw_float_total", frameIndex, total_start);
        return;
    }
    mlv_stage_timing_note_elapsed("raw_float_locked",
                                  frameIndex,
                                  g_mlv_last_raw_uint16_ms + llrpGetLastSharedLockMilliseconds());

    /* convert uint16_t raw data -> float raw_data for processing with amaze or bilinear debayer, both need data input as float */
    const double raw_float_convert_start = mlv_stage_timing_now();
    #pragma omp parallel for
    for (volatile int i = 0; i < pixels_count; ++i)
    {
        outputFrame[i] = (float)(unpacked_frame[i] << shift_val);
    }
    g_mlv_last_raw_float_convert_ms = (mlv_stage_timing_now() - raw_float_convert_start) * 1000.0;
    mlv_stage_timing_note_elapsed("raw_float_convert", frameIndex, g_mlv_last_raw_float_convert_ms);

    mlv_stage_timing_note("raw_float_total", frameIndex, total_start);
}

void setMlvProcessing(mlvObject_t * video, processingObject_t * processing)
{
    //double camera_matrix[9]; commented for now, not used

    /* Easy bit */
    video->processing = processing;
    resetMlvCachedFrame(video);

    /* Link dual_iso value, because it is needed */
    video->processing->dual_iso = &video->llrawproc->dual_iso;

    /* explicitely switch whitebalance find flag off to get right matrix values */
    video->processing->wbFindActive = 0;

    const int processing_width = getMlvWidth(video);
    const int processing_height = getMlvHeight(video);
    size_t processing_pixels = 0;
    if (processing_width > 0 && processing_height > 0
        && (size_t)processing_width <= SIZE_MAX / (size_t)processing_height) {
        processing_pixels = (size_t)processing_width * (size_t)processing_height;
    }

    /* Vignette alloc.  Never retain an undersized old buffer after realloc
     * failure: disable the position-dependent stage instead. */
    float *vignette_mask = processing_pixels > 0
        && processing_pixels <= SIZE_MAX / sizeof(float)
        ? realloc(video->processing->vignette_mask,
                  processing_pixels * sizeof(float))
        : NULL;
    if (vignette_mask) {
        video->processing->vignette_mask = vignette_mask;
    } else {
        free(video->processing->vignette_mask);
        video->processing->vignette_mask = NULL;
        video->processing->vignette_strength = 0;
    }

    /* Gradient alloc follows the same fail-closed ownership rule. */
    uint16_t *gradient_mask = processing_pixels > 0
        && processing_pixels <= SIZE_MAX / sizeof(uint16_t)
        ? realloc(video->processing->gradient_mask,
                  processing_pixels * sizeof(uint16_t))
        : NULL;
    if (gradient_mask) {
        video->processing->gradient_mask = gradient_mask;
    } else {
        free(video->processing->gradient_mask);
        video->processing->gradient_mask = NULL;
        video->processing->gradient_enable = 0;
    }

    /* MATRIX stuff (not working, so commented out -
     * processing object defaults to 1,0,0,0,1,0,0,0,1) */

    /* Get camera matrix for MLV clip and set it in the processing object */
    //getMlvCameraTosRGBMatrix(video, camera_matrix);
    /* Set Camera to RGB */
    //processingCamTosRGBMatrix(processing, camera_matrix); /* Still not used in processing cos not working right */

    /* Make copy of original black and white levels, because it can be changed from the gui */
    video->original_black_level = getMlvBlackLevel(video);
    video->original_white_level = getMlvWhiteLevel(video);

    /* BLACK / WHITE level */
    processingSetBlackAndWhiteLevel( processing, getMlvBlackLevel(video), getMlvWhiteLevel(video), getMlvBitdepth(video) );

    /* If 5D3 or cropmode */
    if (strlen((char *)getMlvCamera(video)) > 20 || getMlvMaxWidth(video) > 1920)
    {
        processingSetSharpeningBias(processing, 0.0);
    }
    else /* Sharpening more sideways to hide vertical line skip artifacts a bit */
    {
        processingSetSharpeningBias(processing, -0.33);
    }

    /* Get camera matrices, daylight and tungsten */
    if (camidCheckIfCameraKnown(getMlvCameraModel(video)))
    {
        double cam_matrix_D[9], cam_matrix_A[9];
        int32_t * cam_matrix_D_int = camidGetColorMatrix2(getMlvCameraModel(video));
        int32_t * cam_matrix_A_int = camidGetColorMatrix1(getMlvCameraModel(video));
        for (int i = 0; i < 9; ++i)
        {
            cam_matrix_D[i] = ((double)cam_matrix_D_int[i*2])/((double)cam_matrix_D_int[i*2+1]);
            cam_matrix_A[i] = ((double)cam_matrix_A_int[i*2])/((double)cam_matrix_A_int[i*2+1]);
        }

        processingSetCamMatrix(processing, cam_matrix_D, cam_matrix_A);
    }
    else
    {
        /* If the camera is unknown, get matrix from the MLV matrix field.
         * Currently, MLV only stores one matrix unfortunately, so same the same
         * matrix will be used for tungsten and daylight. TODO: update this
         * code once MLV has new colour matrix blocks */
        double cam_matrix[9];
        int32_t * mlv_mat = video->RAWI.raw_info.color_matrix1;
        for (int i = 0; i < 9; ++i)
        {
            cam_matrix[i] = ((double)mlv_mat[i*2])/((double)mlv_mat[i*2+1]);
        }
        processingSetCamMatrix(processing, cam_matrix, cam_matrix);
    }
}

/* Phase 4B-v2: TLS scratch buffer for the post-downsample bayer (used
 * before llrawproc runs on the smaller buffer). */
static uint16_t * mlv_ensure_thread_scaled_bayer_buffer(uint64_t required_words)
{
    static MLV_THREAD_LOCAL uint16_t * tls_buffer = NULL;
    static MLV_THREAD_LOCAL uint64_t tls_capacity_words = 0;
    return mlv_ensure_u16_buffer(&tls_buffer, &tls_capacity_words, required_words);
}

/* Phase 4B-v2: env kill switch — set to 1 to disable downsample-before-
 * llrawproc and fall back to the v1 (downsample-after) path. Cached after
 * first read for branchless per-frame fast path. */
static int g_mlv_phase4bv2_disabled_env_cache = -1;

static int mlv_phase4bv2_disabled_via_env(void)
{
    if (g_mlv_phase4bv2_disabled_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISABLE_PHASE4BV2");
        g_mlv_phase4bv2_disabled_env_cache =
            (v && *v && strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
    }
    return g_mlv_phase4bv2_disabled_env_cache;
}

/* Phase 4B-v3: env kill switch — set to 1 to disable the Y-crop-to-16-aligned
 * full XY pre-recon wrapper and force the v2 X-only-pre-recon fallback. v3
 * adds <= 0.66% of source rows lost from the bottom edge (zero on clips
 * already 16-aligned). */
static int g_mlv_phase4bv3_disabled_env_cache = -1;

static int mlv_phase4bv3_disabled_via_env(void)
{
    if (g_mlv_phase4bv3_disabled_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISABLE_PHASE4BV3");
        g_mlv_phase4bv3_disabled_env_cache =
            (v && *v && strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
    }
    return g_mlv_phase4bv3_disabled_env_cache;
}

/* Phase 4B-v4: env kill switch for the explicit x8 full-XY pre-recon path.
 * The path stays on the fast lane only when the clip can remain 32-row
 * aligned; otherwise we fall back to the safer post-recon path so cropped
 * edge blocks do not leak into the settled frame. */
static int g_mlv_phase4bv4_x8_disabled_env_cache = -1;

static int mlv_phase4bv4_x8_disabled_via_env(void)
{
    if (g_mlv_phase4bv4_x8_disabled_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISABLE_PHASE4BV4_X8");
        g_mlv_phase4bv4_x8_disabled_env_cache =
            (v && *v && strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
    }
    return g_mlv_phase4bv4_x8_disabled_env_cache;
}

static int mlv_phase4bv4_x8_preview_compatible(const mlvObject_t * video)
{
    if (!video)
    {
        return 0;
    }

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    return full_w > 0 && full_h >= 32 && (full_w % 16) == 0 && (full_h % 32) == 0;
}

static void mlv_phase4bv2_log_rejection(const char * reason);
static int mlv_render_scaled_rgb16_v4_x8_full_xy(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 uint16_t * outputFrame,
                                                 int threads);
static int mlv_render_scaled_rgb16_x2_full_xy(mlvObject_t * video,
                                              uint64_t frameIndex,
                                              uint16_t * outputFrame,
                                              int threads);

/* Phase 4B quality guard: HQ playback should favor the full post-recon
 * downsample at x4 unless explicitly opted back into the fast pre-recon
 * shortcut. Explicit x8 is already a deep preview tradeoff, so it is allowed
 * to take the reduced-Bayer path even when HQ mean23 is active. */
static int g_mlv_phase4bv2_allow_fast_hq_env_cache = -1;

static int mlv_phase4bv2_allow_fast_hq_via_env(int scaleFactor)
{
    if (scaleFactor == 4 && mlvPlaybackAggressivePreviewMode())
    {
        return 1;
    }
    if (scaleFactor == 2 && mlvPlaybackAggressivePreviewMode())
    {
        return 1;
    }
    if (mlvPlaybackFastX4HqPathMode())
    {
        return 1;
    }
    if (g_mlv_phase4bv2_allow_fast_hq_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_ENABLE_DUAL_ISO_FAST_X4_IN_HQ");
        g_mlv_phase4bv2_allow_fast_hq_env_cache =
            (v && *v && strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
    }
    return g_mlv_phase4bv2_allow_fast_hq_env_cache;
}

/* x2 quality-preserving full-res fix path: apply the full-res raw fix stages
 * before the x2 early reduction path, so receipts that need focus/bad/
 * stripe/pattern corrections can still take the reduced Bayer path without
 * routing through the aggressive skip-fixes approximation.
 *
 * Default-on for x2 sharp/smooth because the profile/smoke matrix showed it
 * is the safer performance win; explicit env false/0 still opts out. */
static int g_mlv_phase4bv2_x2_fullres_fix_env_cache = -1;

static int mlv_phase4bv2_x2_fullres_fix_via_env(void)
{
    if (g_mlv_phase4bv2_x2_fullres_fix_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_ENABLE_DUAL_ISO_X2_FULLRES_FIXES");
        if (!v || !*v)
        {
            g_mlv_phase4bv2_x2_fullres_fix_env_cache = 1;
        }
        else
        {
            g_mlv_phase4bv2_x2_fullres_fix_env_cache =
                (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
        }
    }
    return g_mlv_phase4bv2_x2_fullres_fix_env_cache;
}

/* Quarter-res x2 playback preview: default on for playback, with a kill
 * switch for comparison runs. Read fresh each call so tests can flip it
 * without a cache reset. Round-3 item 1: when the env is unset, the
 * GUI-selected proxy level decides - Full (0) disables the preview core,
 * Auto (-1) / Half (1) / Quarter (2) keep it on (one internal halving is
 * this scale's only proxy step today). */
static int mlv_quarterres_x2_preview_enabled(void)
{
    const char * v = getenv("MLVAPP_DISABLE_QUARTERRES_X2_PREVIEW");
    if (v && *v)
    {
        return (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 0 : 1;
    }
    return (mlvPlaybackProxyLevel() == 0) ? 0 : 1;
}

/* Half-res x1 playback preview: default on for playback, with a kill switch
 * for comparison runs. This is the user-approved x1 quality trade and only
 * applies while playback preview mode is active. Round-3 item 1: when the
 * env is unset, the GUI-selected proxy level decides (Full disables). */
static int mlv_halfres_x1_preview_enabled(void)
{
    const char * v = getenv("MLVAPP_DISABLE_HALFRES_X1_PREVIEW");
    if (v && *v)
    {
        return (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 0 : 1;
    }
    return (mlvPlaybackProxyLevel() == 0) ? 0 : 1;
}

/* Round-3 item 2: run applyProcessingObject at the proxy's half resolution
 * and upscale the PROCESSED result (processing is 94-99% of the x1 slow-
 * decile render; this cuts its pixel count 4x). Only meaningful while the
 * x1 half-res preview proxy itself is engaged. Default on; the kill switch
 * restores the round-2 process-at-full behavior for A/Bs. Fresh read. */
static int mlv_halfres_x1_processing_enabled(void)
{
    const char * v = getenv("MLVAPP_DISABLE_HALFRES_X1_PROCESSING");
    if (v && *v)
    {
        return (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 0 : 1;
    }
    return 1;
}

/* Round-3 item 5: the same lever at x2 Sharp - process at the quarter
 * render's native dims (the proven SH x4 lane via the effective-scale
 * envelope) and upscale the PROCESSED result to the x2 display size.
 * Default on; the kill switch restores process-at-x2-out for A/Bs. */
static int mlv_quarterres_x2_processing_enabled(void)
{
    const char * v = getenv("MLVAPP_DISABLE_QUARTERRES_X2_PROCESSING");
    if (v && *v)
    {
        return (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 0 : 1;
    }
    return 1;
}

static int g_mlv_phase4bv2_x4_fullres_fix_env_cache = -1;

static int mlv_phase4bv2_x4_fullres_fix_via_env(void)
{
    if (g_mlv_phase4bv2_x4_fullres_fix_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_ENABLE_DUAL_ISO_X4_FULLRES_FIXES");
        if (!v || !*v)
        {
            g_mlv_phase4bv2_x4_fullres_fix_env_cache = 1;
        }
        else
        {
            g_mlv_phase4bv2_x4_fullres_fix_env_cache =
                (strcmp(v, "0") != 0 && strcmp(v, "false") != 0) ? 1 : 0;
        }
    }
    return g_mlv_phase4bv2_x4_fullres_fix_env_cache;
}

static int mlv_phase4bv2_quality_allows_pre_recon(mlvObject_t * video,
                                                  int scaleFactor)
{
    if (video && video->llrawproc
     && video->llrawproc->diso_playback_force_mean23 != 0
     && scaleFactor != 8
     && !mlv_phase4bv2_allow_fast_hq_via_env(scaleFactor)
     && !(scaleFactor == 4 && mlv_phase4bv2_x4_fullres_fix_via_env())
     && !(scaleFactor == 2
       && (mlv_quarterres_x2_preview_enabled()
        || mlv_phase4bv2_x2_fullres_fix_via_env())))
    {
        mlv_phase4bv2_log_rejection("HQ mean23 playback uses full-recon x4 fallback");
        return 0;
    }
    return 1;
}

typedef struct
{
    int focus_pixels;
    int bad_pixels;
    int vertical_stripes;
    int pattern_noise;
} mlv_phase4bv2_receipt_flag_snapshot_t;

static void mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(llrawprocObject_t * shared,
                                                                   mlv_phase4bv2_receipt_flag_snapshot_t * snapshot)
{
    if (!shared || !snapshot)
    {
        return;
    }

    snapshot->focus_pixels = shared->focus_pixels;
    snapshot->bad_pixels = shared->bad_pixels;
    snapshot->vertical_stripes = shared->vertical_stripes;
    snapshot->pattern_noise = shared->pattern_noise;

    shared->focus_pixels = 0;
    shared->bad_pixels = 0;
    shared->vertical_stripes = 0;
    shared->pattern_noise = 0;
}

static void mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(llrawprocObject_t * shared,
                                                                     const mlv_phase4bv2_receipt_flag_snapshot_t * snapshot)
{
    if (!shared || !snapshot)
    {
        return;
    }

    shared->focus_pixels = snapshot->focus_pixels;
    shared->bad_pixels = snapshot->bad_pixels;
    shared->vertical_stripes = snapshot->vertical_stripes;
    shared->pattern_noise = snapshot->pattern_noise;
}

/* Phase 4B-v3/v4: telemetry — counts which path the most recent v2 entry took.
 * 8 = v4 x8 full-XY pre-recon (32-row-cropped if necessary)
 * 4 = x2 full-XY pre-recon (8-row-cropped if necessary)
 * 3 = full XY (v3, Y-cropped or natively aligned); 2 = X-only fallback;
 * 0 = v2 entry not invoked / rejected before path selection.
 *
 * Process-wide (not thread-local) so test code reading the counter on the
 * test thread sees writes from any worker thread that ran the v2 entry.
 * The counter is updated atomically via store-only semantics; tests don't
 * require any cross-thread synchronisation beyond a memory fence at the
 * end of the rendering call (which is provided by the OMP parallel-for
 * end barrier in the surrounding pipeline). */
static MLV_STAGE_THREAD_LOCAL int g_mlv_phase4bv2_path_taken = 0;
static MLV_STAGE_THREAD_LOCAL int g_mlv_phase4bv3_y_crop_rows = 0;
static MLV_STAGE_THREAD_LOCAL const char * g_mlv_phase4bv2_last_fallback_reason = "none";

int mlv_phase4bv2_last_path_taken(void)
{
    return g_mlv_phase4bv2_path_taken;
}

const char * mlv_phase4bv2_last_fallback_reason(void)
{
    return g_mlv_phase4bv2_last_fallback_reason
        ? g_mlv_phase4bv2_last_fallback_reason
        : "unknown";
}

int mlv_phase4bv3_last_y_crop_rows(void)
{
    return g_mlv_phase4bv3_y_crop_rows;
}

void mlv_phase4bv_reset_env_cache_for_testing(void)
{
    g_mlv_phase4bv2_disabled_env_cache = -1;
    g_mlv_phase4bv3_disabled_env_cache = -1;
    g_mlv_phase4bv4_x8_disabled_env_cache = -1;
    g_mlv_phase4bv2_allow_fast_hq_env_cache = -1;
    g_mlv_phase4bv2_x2_fullres_fix_env_cache = -1;
    g_mlv_phase4bv2_x4_fullres_fix_env_cache = -1;
    mlvSetPlaybackFastX4HqPathMode(0);
}

/* Phase 4B-v2: returns 1 if the receipt's llrawproc options are compatible
 * with the downsample-before-llrawproc path. The path skips focus pixel,
 * bad pixel, vertical stripes, and pattern noise — those need full-res
 * absolute coordinates or column indices. Sharp/Smooth playback falls back
 * when any are enabled; Aggressive Performance preview may skip them as an
 * explicit coarse-preview approximation. Dual ISO HQ recon is the headline
 * case; if it's enabled and the path is otherwise compatible, v2 is safe. */
static int mlv_phase4bv2_receipt_compatible(mlvObject_t * video)
{
    if (!video || !video->llrawproc) return 0;
    llrawprocObject_t * shared = video->llrawproc;
    if (mlvPlaybackAggressivePreviewMode()
     && (shared->focus_pixels
      || shared->bad_pixels
      || shared->vertical_stripes
      || shared->pattern_noise))
    {
        return 1;
    }
    if (shared->focus_pixels) return 0;
    if (shared->bad_pixels) return 0;
    if (shared->vertical_stripes) return 0;
    if (shared->pattern_noise) return 0;
    return 1;
}

/* Phase 4B-v2: one-shot diagnostic logger. Set MLVAPP_LOG_PHASE4BV2=1 to
 * see why the v2 path is being rejected on the first frame of a session. */
static int g_mlv_phase4bv2_log_env_cache = -1;
static MLV_THREAD_LOCAL int g_mlv_phase4bv2_log_emitted = 0;

static void mlv_phase4bv2_set_fallback_reason(const char * reason)
{
    g_mlv_phase4bv2_last_fallback_reason = reason ? reason : "unknown";
}

static int mlv_phase4bv2_fallback_reason_is_none(void)
{
    return !g_mlv_phase4bv2_last_fallback_reason
        || strcmp(g_mlv_phase4bv2_last_fallback_reason, "none") == 0;
}

static void mlv_phase4bv2_set_fallback_reason_if_none(const char * reason)
{
    if (mlv_phase4bv2_fallback_reason_is_none())
    {
        mlv_phase4bv2_set_fallback_reason(reason);
    }
}

static void mlv_phase4bv2_log_rejection(const char * reason)
{
    mlv_phase4bv2_set_fallback_reason(reason);

    if (g_mlv_phase4bv2_log_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_LOG_PHASE4BV2");
        g_mlv_phase4bv2_log_env_cache =
            (v && *v && strcmp(v, "0") != 0) ? 1 : 0;
    }
    if (!g_mlv_phase4bv2_log_env_cache) return;
    if (g_mlv_phase4bv2_log_emitted) return;
    g_mlv_phase4bv2_log_emitted = 1;
    fprintf(stderr, "[PHASE4BV2] reject: %s\n", reason);
    fflush(stderr);
}

/* Phase 4B-v3 (full XY pre-recon, optionally Y-cropped to 16-aligned):
 *
 * Runs pl_downsample_bayer_to_bayer_4x on (full_w, eff_h) where
 * eff_h = (full_h / 16) * 16 — the largest 16-aligned height <= full_h.
 * For clips where full_h is already 16-aligned this is identity (no rows
 * lost). For clips where it isn't (e.g. user's 5K M16-1210 at 1808x2268,
 * gcd=4), eff_h = 2256, losing 12 source rows from the bottom edge
 * (~0.5% Y loss, invisible at playback display resolution after the
 * existing bilinear stretch).
 *
 * After the kernel produces (full_w/4, eff_h/4) bayer, llrawproc and
 * debayer run on that smaller buffer. The output rows 0..eff_h/4-1 are
 * direct recon results; the trailing (full_h - eff_h)/4 rows of the
 * caller-sized output buffer are filled by replicating the last valid
 * row, so the downstream upscale doesn't see a black band.
 *
 * Returns 1 on success, 0 to fall back to the v2 X-only path. */
static int mlv_render_scaled_rgb16_v3_full_xy(mlvObject_t * video,
                                              uint64_t frameIndex,
                                              uint16_t * outputFrame,
                                              int threads,
                                              int * out_dn_ms_x100)
{
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);

    /* in_w must be multiple of 4 (kernel constraint, same as v2).
     * eff_h must be multiple of 16 (kernel constraint), so we crop down to
     * the nearest 16-aligned height. eff_h must also be > 0; in practice
     * any sensible Dual ISO clip has full_h >= 16. */
    const int eff_h = (full_h / 16) * 16;
    if (full_w % 4 != 0 || eff_h < 16) return 0;

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    /* Bayer→bayer 4x kernel output dim. */
    const int mid_w = full_w / 4;
    const int mid_h = eff_h / 4;
    /* Final output dim that the caller's buffer is sized for. */
    const int out_w = full_w / 4;
    const int out_h = full_h / 4;
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t mid_rgb_words = mid_pixels * 3u;
    const uint64_t out_rgb_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    uint16_t * full_bayer = mlv_ensure_thread_u16_buffer(full_pixels);
    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    uint16_t * mid_rgb = mlv_ensure_thread_rgb_u16_buffer(mid_rgb_words);
    if (!full_bayer || !mid_bayer || !mid_rgb) return 0;

    /* Step 1: decode raw at full res (no llrawproc). */
    const double raw_start = mlv_stage_timing_now();
    if (getMlvRawFrameUint16(video, frameIndex, full_bayer))
    {
        memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
        return 0;
    }
    g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);

    /* Seed the dual-ISO row pattern from the full-res raw before downsampling
     * (mirrors the v4_x8 core), so the x4 scaled recon does not mis-detect it
     * from 4x-downsampled data (the cold-pass pink). */
    llrpEnsureDualIsoPatternSeeded(video, full_bayer, full_w, full_h);

    /* Step 2: bayer→bayer 4x (full XY) downsample, viewing the full bayer
     * as (full_w, eff_h). The kernel reads only rows 0..eff_h-1 — rows
     * eff_h..full_h-1 are simply ignored. (Reading less data than the
     * buffer holds is safe.) */
    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = pl_downsample_bayer_to_bayer_4x(full_bayer, full_w, eff_h,
                                              mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection("v3 bayer-to-bayer 4x kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    /* Step 3: apply llrawproc subset on the narrowed buffer. Dimensions
     * (mid_w, mid_h) — the dual-ISO 4-row pattern is preserved by the
     * kernel because it samples complete 4-row blocks at stride 16 in
     * source space. */
    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                  mid_w, mid_h);
    if (!llraw_ok)
    {
        mlv_phase4bv2_log_rejection("v3 applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    /* Step 4: debayer narrowed bayer → RGB16 at (mid_w, mid_h) directly
     * into the head of the output buffer. (out_w == mid_w by design,
     * and mid_h <= out_h, so this fills rows 0..mid_h-1 of the output.) */
    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(outputFrame, mid_bayer, mid_w, mid_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    /* Step 5: replicate the last valid row into rows mid_h..out_h-1 of the
     * output buffer. For 16-aligned clips mid_h == out_h and this loop
     * runs zero iterations. For the user's 1808x2268 clip mid_h=564,
     * out_h=567 — we replicate row 563 into rows 564, 565, 566. The
     * downstream bilinear upscale to display target then sees a 567-row
     * image; the last 3 rows are duplicates of row 563, contributing a
     * negligible band at the very bottom edge after stretch. */
    const size_t row_words = (size_t)out_w * 3u;
    if (mid_h > 0)
    {
        const uint16_t * last_row = outputFrame + (size_t)(mid_h - 1) * row_words;
        for (int y = mid_h; y < out_h; ++y)
        {
            uint16_t * dst = outputFrame + (size_t)y * row_words;
            memcpy(dst, last_row, row_words * sizeof(uint16_t));
        }
    }
    else
    {
        memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
    }

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    if (out_dn_ms_x100)
    {
        *out_dn_ms_x100 = (int)(downsample_ms * 100.0);
    }
    return 1;
}

/* Aggressive x2 full-XY pre-recon path.
 *
 * This applies the same early-resolution contract as x4/x8: reduce the
 * raw Bayer buffer before llrawproc/Dual ISO and before debayer. The
 * 2x Bayer-preserving kernel keeps complete 4-row Dual ISO phase groups
 * at source block stride 8 and crops the source height down to the nearest
 * 8-row boundary, then fills any missing output rows from the last valid
 * row. It is gated by Aggressive Performance preview; Sharp/Smooth x2 keeps
 * the full-recon path for the less-approximated preview. */
static int mlv_render_scaled_rgb16_x2_full_xy_core(mlvObject_t * video,
                                                   uint64_t frameIndex,
                                                   const uint16_t * decodedRawFrame,
                                                   uint16_t * outputFrame,
                                                   int threads)
{
    if (!video || !outputFrame) return 0;

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 8) * 8;
    if ((full_w % 4) != 0 || eff_h < 8)
    {
        mlv_phase4bv2_log_rejection("x2 bayer-to-bayer dims rejected");
        return 0;
    }

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    const int mid_w = full_w / 2;
    const int mid_h = eff_h / 2;
    const int out_w = full_w / 2;
    const int out_h = full_h / 2;
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t out_rgb_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    const uint16_t * raw_source = decodedRawFrame;
    uint16_t * full_bayer = NULL;
    if (!raw_source)
    {
        full_bayer = mlv_ensure_thread_u16_buffer(full_pixels);
        if (!full_bayer) return 0;

        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, full_bayer))
        {
            memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
            return 0;
        }
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
        raw_source = full_bayer;
    }

    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    if (!mid_bayer) return 0;

    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = pl_downsample_bayer_to_bayer_2x(raw_source, full_w, eff_h,
                                             mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection("x2 bayer-to-bayer kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                  mid_w, mid_h);
    if (!llraw_ok)
    {
        mlv_phase4bv2_log_rejection("x2 applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(outputFrame, mid_bayer, mid_w, mid_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    const size_t row_words = (size_t)out_w * 3u;
    if (mid_h > 0)
    {
        const uint16_t * last_row = outputFrame + (size_t)(mid_h - 1) * row_words;
        for (int y = mid_h; y < out_h; ++y)
        {
            uint16_t * dst = outputFrame + (size_t)y * row_words;
            memcpy(dst, last_row, row_words * sizeof(uint16_t));
        }
    }
    else
    {
        memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
    }

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    g_mlv_phase4bv2_path_taken = 4;
    return 1;
}

static int mlv_render_scaled_rgb16_x2_full_xy(mlvObject_t * video,
                                              uint64_t frameIndex,
                                              uint16_t * outputFrame,
                                              int threads)
{
    return mlv_render_scaled_rgb16_x2_full_xy_core(video, frameIndex, NULL,
                                                  outputFrame, threads);
}

static int mlv_render_scaled_rgb16_x2_full_xy_from_raw(mlvObject_t * video,
                                                       uint64_t frameIndex,
                                                       const uint16_t * decodedRawFrame,
                                                       uint16_t * outputFrame,
                                                       int threads)
{
    if (!decodedRawFrame) return 0;
    return mlv_render_scaled_rgb16_x2_full_xy_core(video, frameIndex, decodedRawFrame,
                                                  outputFrame, threads);
}

/* Edge-clamped bilinear upscale of an interleaved RGB16 image to an explicit
 * target size. Used to expand the quarter-res recon to the x2 display size. */
static void mlv_rgb_u16_upscale_to_size(const uint16_t * __restrict src,
                                        int src_w, int src_h,
                                        uint16_t * __restrict dst,
                                        int dst_w, int dst_h, int threads)
{
    if (!src || !dst || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) return;
    const size_t src_stride = (size_t)src_w * 3u;
    const size_t dst_stride = (size_t)dst_w * 3u;
    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < dst_h; ++y)
    {
        int y0 = y >> 1; if (y0 >= src_h) y0 = src_h - 1;
        const int y_odd = y & 1;
        const int y1 = (y_odd && y0 + 1 < src_h) ? (y0 + 1) : y0;
        const uint16_t * row0 = src + (size_t)y0 * src_stride;
        const uint16_t * row1 = src + (size_t)y1 * src_stride;
        uint16_t * drow = dst + (size_t)y * dst_stride;
        for (int x = 0; x < dst_w; ++x)
        {
            int x0 = x >> 1; if (x0 >= src_w) x0 = src_w - 1;
            const int x_odd = x & 1;
            const int x1 = (x_odd && x0 + 1 < src_w) ? (x0 + 1) : x0;
            const uint16_t * p00 = row0 + (size_t)x0 * 3u;
            const uint16_t * p01 = row0 + (size_t)x1 * 3u;
            const uint16_t * p10 = row1 + (size_t)x0 * 3u;
            const uint16_t * p11 = row1 + (size_t)x1 * 3u;
            uint16_t * out = drow + (size_t)x * 3u;
            if (x_odd && y_odd)
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + p01[0] + p10[0] + p11[0]) >> 2);
                out[1] = (uint16_t)(((uint32_t)p00[1] + p01[1] + p10[1] + p11[1]) >> 2);
                out[2] = (uint16_t)(((uint32_t)p00[2] + p01[2] + p10[2] + p11[2]) >> 2);
            }
            else if (x_odd)
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + p01[0]) >> 1);
                out[1] = (uint16_t)(((uint32_t)p00[1] + p01[1]) >> 1);
                out[2] = (uint16_t)(((uint32_t)p00[2] + p01[2]) >> 1);
            }
            else if (y_odd)
            {
                out[0] = (uint16_t)(((uint32_t)p00[0] + p10[0]) >> 1);
                out[1] = (uint16_t)(((uint32_t)p00[1] + p10[1]) >> 1);
                out[2] = (uint16_t)(((uint32_t)p00[2] + p10[2]) >> 1);
            }
            else
            {
                out[0] = p00[0]; out[1] = p00[1]; out[2] = p00[2];
            }
        }
    }
}

/* Quarter-res x2 preview core: 4x Bayer downsample -> HQ Dual ISO recon at
 * quarter res -> debayer -> 2x bilinear upscale to the x2 display size. Mirrors
 * the buffer discipline of the proven x2 / v3 cores. Returns 1 on success, 0 to
 * fall back to the half-res x2 path.
 * Round-3 item 5: when upscale_to_out is 0 the core stops after the debayer
 * + row padding and leaves the QUARTER image at the start of outputFrame
 * (dims via the out-params); the caller processes at quarter dims (the
 * proven SH x4 lane via the effective-scale envelope) and upscales the
 * PROCESSED result - the same pattern that took x1 from ~9 to ~17. */
static int mlv_render_scaled_rgb16_x2_quarter_preview_core(mlvObject_t * video,
                                                           uint64_t frameIndex,
                                                           const uint16_t * decodedRawFrame,
                                                           uint16_t * outputFrame,
                                                           int threads,
                                                           int upscale_to_out,
                                                           int * out_q_w,
                                                           int * out_q_h)
{
    if (!video || !outputFrame) return 0;
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 16) * 16;            /* 16-aligned: dual-ISO 4-row phase */
    if ((full_w % 4) != 0 || eff_h < 16) return 0;

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    const int q_w = full_w / 4;
    const int q_h = eff_h / 4;
    const int q_out_h = full_h / 4;                  /* padded quarter height */
    const int x2_out_w = full_w / 2;
    const int x2_out_h = full_h / 2;                 /* x2 display dims (matches x2 core) */
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t q_pixels = (uint64_t)q_w * (uint64_t)q_h;
    const uint64_t q_rgb_words = (uint64_t)q_w * (uint64_t)q_out_h * 3u;

    const uint16_t * raw_source = decodedRawFrame;
    uint16_t * full_bayer = NULL;
    if (!raw_source)
    {
        full_bayer = mlv_ensure_thread_u16_buffer(full_pixels);
        if (!full_bayer) return 0;
        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, full_bayer)) return 0;
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
        raw_source = full_bayer;
    }

    uint16_t * q_bayer = mlv_ensure_thread_scaled_bayer_buffer(q_pixels);
    if (!q_bayer) return 0;
    const double downsample_start = mlv_stage_timing_now();
    int actual_q_w = 0, actual_q_h = 0;
    if (pl_downsample_bayer_to_bayer_4x(raw_source, full_w, eff_h, q_bayer,
                                        &actual_q_w, &actual_q_h, threads) != 0
        || actual_q_w != q_w || actual_q_h != q_h)
    {
        mlv_phase4bv2_log_rejection("x2-quarter bayer 4x kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t q_bytes = (size_t)q_pixels * sizeof(uint16_t);
    if (!applyLLRawProcObject_with_dims(video, q_bayer, q_bytes, q_w, q_h))
    {
        mlv_phase4bv2_log_rejection("x2-quarter applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    if (!upscale_to_out)
    {
        /* Reduced-processing mode: debayer + pad straight into the caller's
         * buffer at quarter layout; the caller upscales after processing. */
        const double debayer_start = mlv_stage_timing_now();
        debayerBasicU16(outputFrame, q_bayer, q_w, q_h, threads, bit_shift);
        const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
        const size_t q_row_words_direct = (size_t)q_w * 3u;
        if (q_h > 0)
        {
            const uint16_t * last_row =
                outputFrame + (size_t)(q_h - 1) * q_row_words_direct;
            for (int y = q_h; y < q_out_h; ++y)
            {
                memcpy(outputFrame + (size_t)y * q_row_words_direct,
                       last_row, q_row_words_direct * sizeof(uint16_t));
            }
        }
        g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
        mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
        if (out_q_w) *out_q_w = q_w;
        if (out_q_h) *out_q_h = q_out_h;
        g_mlv_phase4bv2_path_taken = 11;             /* x2-quarter preview, quarter processing */
        return 1;
    }

    uint16_t * q_rgb = mlv_ensure_thread_rgb_u16_buffer(q_rgb_words);
    if (!q_rgb) return 0;
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(q_rgb, q_bayer, q_w, q_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    /* Pad the cropped rows (q_h .. q_out_h) from the last valid row, matching v3. */
    const size_t q_row_words = (size_t)q_w * 3u;
    if (q_h > 0)
    {
        const uint16_t * last_row = q_rgb + (size_t)(q_h - 1) * q_row_words;
        for (int y = q_h; y < q_out_h; ++y)
        {
            memcpy(q_rgb + (size_t)y * q_row_words, last_row, q_row_words * sizeof(uint16_t));
        }
    }

    const double upscale_start = mlv_stage_timing_now();
    mlv_rgb_u16_upscale_to_size(q_rgb, q_w, q_out_h, outputFrame, x2_out_w, x2_out_h, threads);
    const double upscale_ms = (mlv_stage_timing_now() - upscale_start) * 1000.0;

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms + upscale_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    if (out_q_w) *out_q_w = q_w;
    if (out_q_h) *out_q_h = q_out_h;
    g_mlv_phase4bv2_path_taken = 5;                  /* x2-quarter preview */
    return 1;
}

/* Half-res x1 preview core: 2x Bayer downsample -> HQ Dual ISO recon at half
 * res -> debayer -> 2x bilinear upscale back to the full display size.
 * Mirrors the preview-only tradeoff pattern used by the x2 quarter-res path,
 * but keeps the user-facing output at full dimensions. Returns 1 on success,
 * 0 to fall back to the original full-res x1 path. */
/* Round-3 item 2: when upscale_to_full is 0, the core stops after the
 * debayer and leaves the REDUCED image at the start of outputFrame (mid_w x
 * mid_h reported via the out-params); the caller then runs processing at mid
 * dimensions and upscales the PROCESSED result, cutting the dominant
 * processing cost. With upscale_to_full=1 the round-2 behavior is
 * byte-identical (full-size output, out-params optional).
 * Round-3 item 3: halvings selects the proxy depth - 1 = half res (2x bayer
 * kernel), 2 = quarter res (4x kernel, needs full_w%8==0 and eff_h>=32;
 * the caller degrades to halvings=1 when rejected). The full-size upscale
 * for halvings=2 runs as two verified 2x passes. */
static int mlv_render_scaled_rgb16_x1_half_preview_core(mlvObject_t * video,
                                                        uint64_t frameIndex,
                                                        const uint16_t * decodedRawFrame,
                                                        uint16_t * outputFrame,
                                                        int threads,
                                                        int upscale_to_full,
                                                        int halvings,
                                                        int * out_mid_w,
                                                        int * out_mid_h)
{
    if (!video || !outputFrame || !llrpHQDualIso(video)) return 0;
    if (halvings != 1 && halvings != 2) return 0;
    if (!mlv_phase4bv2_receipt_compatible(video))
    {
        mlv_phase4bv2_log_rejection(halvings == 2
                                    ? "x1 quarter-res receipt incompatible"
                                    : "x1 half-res receipt incompatible");
        return 0;
    }
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 16) * 16;            /* 16-aligned: dual-ISO 4-row phase */
    if ((full_w % 4) != 0 || eff_h < 16) return 0;
    if (halvings == 2 && ((full_w % 8) != 0 || eff_h < 32)) return 0;

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    const int mid_w = full_w >> halvings;
    const int mid_h = eff_h >> halvings;
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t mid_rgb_words = mid_pixels * 3u;

    const uint16_t * raw_source = decodedRawFrame;
    uint16_t * full_bayer = NULL;
    if (!raw_source)
    {
        full_bayer = mlv_ensure_thread_u16_buffer(full_pixels);
        if (!full_bayer) return 0;

        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, full_bayer))
        {
            return 0;
        }
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
        raw_source = full_bayer;
    }

    /* Seed the dual-ISO row pattern from the full-res raw before reduction so
     * the half-res recon sees the same row phase contract as the deeper
     * pre-recon paths. The 4x reduction (halvings==2) must NOT seed here:
     * the proven x2 quarter core runs the 4x kernel unseeded, and seeding
     * before a 4x Y-reduction mis-phases the bright/dark row pairing, which
     * biases the recon's exposure fusion dark (~30% luminance loss, caught
     * by the round-3 item-3 balance trace). */
    if (halvings == 1)
    {
        llrpEnsureDualIsoPatternSeeded(video, (uint16_t *)raw_source, full_w, full_h);
    }

    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    if (!mid_bayer) return 0;

    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = (halvings == 2)
        ? pl_downsample_bayer_to_bayer_4x(raw_source, full_w, eff_h,
                                          mid_bayer, &actual_mid_w, &actual_mid_h, threads)
        : pl_downsample_bayer_to_bayer_2x(raw_source, full_w, eff_h,
                                          mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection(halvings == 2
                                    ? "x1 quarter-res bayer 4x kernel rejected"
                                    : "x1 half-res bayer 2x kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    if (!applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes, mid_w, mid_h))
    {
        mlv_phase4bv2_log_rejection("x1 half-res applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    if (!upscale_to_full)
    {
        /* Reduced-processing mode: debayer straight into the caller's buffer
         * at mid layout; no upscale here (the caller upscales after
         * processing). */
        const double debayer_start = mlv_stage_timing_now();
        debayerBasicU16(outputFrame, mid_bayer, mid_w, mid_h, threads, bit_shift);
        const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
        g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
        mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
        if (out_mid_w) *out_mid_w = mid_w;
        if (out_mid_h) *out_mid_h = mid_h;
        /* 7 = x1-half preview + half processing; 9 = x1-quarter + quarter. */
        g_mlv_phase4bv2_path_taken = (halvings == 2) ? 9 : 7;
        return 1;
    }

    uint16_t * mid_rgb = mlv_ensure_thread_rgb_u16_buffer(mid_rgb_words);
    if (!mid_rgb) return 0;
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(mid_rgb, mid_bayer, mid_w, mid_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    const double upscale_start = mlv_stage_timing_now();
    if (halvings == 2)
    {
        /* Two verified 2x passes: quarter -> half (bayer-side TLS scratch,
         * dead after the decode above) -> full. */
        const int half_w = full_w / 2;
        const int half_h = full_h / 2;
        uint16_t * tmp_half = mlv_ensure_thread_u16_buffer(
            (uint64_t)half_w * (uint64_t)half_h * 3u);
        if (!tmp_half) return 0;
        mlv_rgb_u16_upscale_to_size(mid_rgb, mid_w, mid_h, tmp_half, half_w, half_h, threads);
        mlv_rgb_u16_upscale_to_size(tmp_half, half_w, half_h, outputFrame, full_w, full_h, threads);
    }
    else
    {
        mlv_rgb_u16_upscale_to_size(mid_rgb, mid_w, mid_h, outputFrame, full_w, full_h, threads);
    }
    const double upscale_ms = (mlv_stage_timing_now() - upscale_start) * 1000.0;

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms + upscale_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    if (out_mid_w) *out_mid_w = mid_w;
    if (out_mid_h) *out_mid_h = mid_h;
    /* 6 = x1-half preview (full-res processing); 10 = x1-quarter variant. */
    g_mlv_phase4bv2_path_taken = (halvings == 2) ? 10 : 6;
    return 1;
}

static int mlv_render_scaled_rgb16_v3_full_xy_from_raw(mlvObject_t * video,
                                                       uint64_t frameIndex,
                                                       const uint16_t * decodedRawFrame,
                                                       uint16_t * outputFrame,
                                                       int threads,
                                                       int * out_dn_ms_x100);

/* Phase 4B-v2: downsample-BEFORE-llrawproc path.
 *
 * Architecture (the actual cast-closed-fast path):
 *   1. Decode raw at full res (LJ92 / unpacked uint16 bayer).
 *   2. Bayer→bayer 4x downsample. Phase 4B-v3 runs the FULL XY downsample
 *      (Y reduced 4x in pre-recon) when in_h is a multiple of 16 OR can
 *      be cropped down to the nearest 16-aligned height with negligible
 *      visual loss. Otherwise falls back to the v2 X-only kernel
 *      (Y identity).
 *   3. Apply HQ Dual ISO recon + dark frame + chroma smooth on the
 *      narrowed buffer.
 *   4. Debayer the narrowed bayer → RGB16.
 *   5. (X-only fallback only) RGB Y-only 4x downsample to final dim.
 *
 * Why the v3 wrapper: the dual-ISO 4-row bright/dark pattern uses
 * is_bright[y%4] inside diso_get_full20bit. Y downsampling pre-recon
 * requires keeping COMPLETE 4-row blocks at stride 16 in source space
 * (so the kept row indices modulo 4 match the iso_patterns cycle). For
 * arbitrary in_h the largest stride that divides in_h is gcd(in_h, 16).
 * For the user's M16-1210 (1808x2268), gcd(2268, 16) = 4 — meaning
 * Y stride 4 = no Y reduction in the v2 X-only fallback. v3 instead
 * crops to eff_h = 2256 (the largest 16-aligned <= 2268), losing 12
 * source rows from the bottom edge, and runs the full XY downsample.
 *
 * Speedup at v3 (full XY) on 5K: HQ recon scales linearly with W*H.
 * Going from W*H to (W/4)*(H/4) saves ~94% of recon time vs the v1
 * post-recon path; ~75% vs the v2 X-only path. The Y-crop is the
 * architectural change needed to push cast-closed playback past
 * ~15 fps GUI on the user's clip.
 *
 * Returns 1 on success, 0 if the receipt is incompatible (caller falls
 * back to v1). */
static int mlv_render_scaled_rgb16_v2(mlvObject_t * video,
                                      uint64_t frameIndex,
                                      uint16_t * outputFrame,
                                      int scaleFactor,
                                      int threads)
{
    g_mlv_phase4bv2_path_taken = 0;
    g_mlv_phase4bv3_y_crop_rows = 0;
    g_mlv_phase4bv2_last_fallback_reason = "none";

    if (!video || !outputFrame || scaleFactor <= 1) return 0;
    if (mlv_phase4bv2_disabled_via_env())
    {
        mlv_phase4bv2_log_rejection("MLVAPP_DISABLE_PHASE4BV2 set");
        return 0;
    }
    if (scaleFactor == 2
     && !mlvPlaybackAggressivePreviewMode()
     && !mlv_phase4bv2_x2_fullres_fix_via_env()
     && !mlv_quarterres_x2_preview_enabled())
    {
        /* Sharp/Smooth x2 falls back only when neither preview shortcut is enabled. */
        mlv_phase4bv2_log_rejection("scale=2 uses full-recon post-downsample fallback");
        return 0;
    }
    if (!mlv_phase4bv2_quality_allows_pre_recon(video, scaleFactor))
    {
        return 0;
    }
    const int x2FullResFixesActive =
        (scaleFactor == 2 && !mlvPlaybackAggressivePreviewMode() && mlv_phase4bv2_x2_fullres_fix_via_env());
    const int x4FullResFixesActive =
        (scaleFactor == 4 && !mlvPlaybackAggressivePreviewMode() && mlv_phase4bv2_x4_fullres_fix_via_env());
    if (!mlv_phase4bv2_receipt_compatible(video))
    {
        if (!x2FullResFixesActive && !x4FullResFixesActive)
        {
            llrawprocObject_t * shared = video ? video->llrawproc : NULL;
            if (!shared) mlv_phase4bv2_log_rejection("no shared");
            else if (shared->focus_pixels) mlv_phase4bv2_log_rejection("focus_pixels enabled");
            else if (shared->bad_pixels) mlv_phase4bv2_log_rejection("bad_pixels enabled");
            else if (shared->vertical_stripes) mlv_phase4bv2_log_rejection("vertical_stripes enabled");
            else if (shared->pattern_noise) mlv_phase4bv2_log_rejection("pattern_noise enabled");
            else mlv_phase4bv2_log_rejection("receipt incompatible (unknown)");
            return 0;
        }
    }
    if (scaleFactor == 8)
    {
        if (mlv_phase4bv4_x8_disabled_via_env())
        {
            mlv_phase4bv2_log_rejection("MLVAPP_DISABLE_PHASE4BV4_X8 set");
            return 0;
        }
        if (!mlv_phase4bv4_x8_preview_compatible(video))
        {
            mlv_phase4bv2_log_rejection("x8 preview requires 32-row aligned height");
            return 0;
        }
        if (mlv_render_scaled_rgb16_v4_x8_full_xy(video, frameIndex, outputFrame, threads))
        {
            return 1;
        }
        return 0;
    }

    if (scaleFactor == 2)
    {
        if (x2FullResFixesActive && !mlv_phase4bv2_receipt_compatible(video))
        {
            const int x2_full_w = (int)getMlvWidth(video);
            const int x2_full_h = (int)getMlvHeight(video);
            const uint64_t full_pixels = (uint64_t)x2_full_w * (uint64_t)x2_full_h;
            const size_t full_bytes = (size_t)full_pixels * sizeof(uint16_t);
            uint16_t * x2_fix_buffer = mlv_ensure_thread_u16_buffer(full_pixels);
            mlv_phase4bv2_receipt_flag_snapshot_t x2_receipt_flags_snapshot = { 0 };
            if (!x2_fix_buffer)
            {
                mlv_phase4bv2_log_rejection("x2 full-res fix buffer rejected");
            }
            else
            {
                const double raw_start = mlv_stage_timing_now();
                if (getMlvRawFrameUint16(video, frameIndex, x2_fix_buffer))
                {
                    mlv_phase4bv2_log_rejection("x2 full-res fix raw decode rejected");
                }
                else
                {
                    g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
                    mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
                    applyLLRawProcObjectPreDualIsoFixes(video, x2_fix_buffer, full_bytes);
                    mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                           &x2_receipt_flags_snapshot);
                    if (mlv_render_scaled_rgb16_x2_full_xy_from_raw(video,
                                                                   frameIndex,
                                                                   x2_fix_buffer,
                                                                   outputFrame,
                                                                   threads))
                    {
                        mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                 &x2_receipt_flags_snapshot);
                        mlv_phase4bv2_set_fallback_reason("none");
                        return 1;
                    }
                    mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                             &x2_receipt_flags_snapshot);
                }
            }
        }
        if (!mlvPlaybackAggressivePreviewMode()
         && mlv_phase4bv2_allow_fast_hq_via_env(scaleFactor)
         && mlv_render_scaled_rgb16_x2_full_xy(video, frameIndex, outputFrame, threads))
        {
            return 1;
        }
        if (!mlvPlaybackAggressivePreviewMode()
         && mlv_quarterres_x2_preview_enabled()
         && mlv_render_scaled_rgb16_x2_quarter_preview_core(video, frameIndex, NULL, outputFrame, threads,
                                                            /*upscale_to_out*/1, NULL, NULL))
        {
            return 1;
        }
        if (mlv_render_scaled_rgb16_x2_full_xy(video, frameIndex, outputFrame, threads))
        {
            return 1;
        }
        return 0;
    }

    if (scaleFactor != 4)
    {
        mlv_phase4bv2_log_rejection("unsupported pre-recon scale");
        return 0;
    }

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    if (full_w <= 0 || full_h <= 0)
    {
        mlv_phase4bv2_log_rejection("invalid dims");
        return 0;
    }
    if (full_w % 4 != 0)
    {
        mlv_phase4bv2_log_rejection("width not multiple of 4");
        return 0;
    }
    if (full_h % 4 != 0)
    {
        mlv_phase4bv2_log_rejection("height not multiple of 4");
        return 0;
    }

    const int x4ReceiptBypassActive = x4FullResFixesActive && !mlv_phase4bv2_receipt_compatible(video);
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const size_t full_bytes = (size_t)full_pixels * sizeof(uint16_t);
    const uint16_t * x4_source = NULL;
    uint16_t * x4_fix_buffer = NULL;
    mlv_phase4bv2_receipt_flag_snapshot_t x4_receipt_flags_snapshot = { 0 };

    if (x4ReceiptBypassActive)
    {
        x4_fix_buffer = mlv_ensure_thread_u16_buffer(full_pixels);
        if (!x4_fix_buffer)
        {
            mlv_phase4bv2_log_rejection("x4 full-res fix buffer rejected");
            return 0;
        }

        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, x4_fix_buffer))
        {
            mlv_phase4bv2_log_rejection("x4 full-res fix raw decode rejected");
            return 0;
        }
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);

        applyLLRawProcObjectPreDualIsoFixes(video, x4_fix_buffer, full_bytes);
        x4_source = x4_fix_buffer;
        mlv_phase4bv2_set_fallback_reason("none");
    }

    /* Phase 4B-v3: try the full XY pre-recon path first. Eligible whenever
     * full_h >= 16 (so eff_h = floor(full_h/16)*16 >= 16) and the v3 kill
     * switch is not set. The path runs the recon on (W/4, eff_h/4) — for
     * 5K Dual ISO (1808x2268) that's (452, 564) = 254,928 pixels, vs the
     * X-only path's (452, 2268) = 1,025,136 pixels. */
    if (!mlv_phase4bv3_disabled_via_env() && full_h >= 16)
    {
        int dn_x100 = 0;
        if (x4_source)
        {
            mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                   &x4_receipt_flags_snapshot);
            if (mlv_render_scaled_rgb16_v3_full_xy_from_raw(video, frameIndex, x4_source, outputFrame, threads, &dn_x100))
            {
                mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                         &x4_receipt_flags_snapshot);
                g_mlv_phase4bv2_path_taken = 3;
                return 1;
            }
            mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                     &x4_receipt_flags_snapshot);
        }
        else if (mlv_render_scaled_rgb16_v3_full_xy(video, frameIndex, outputFrame, threads, &dn_x100))
        {
            g_mlv_phase4bv2_path_taken = 3;
            return 1;
        }
        /* v3 path attempted and rejected — fall through to v2 X-only. */
    }

    /* Phase 4B-v2 X-only fallback. Reached when v3 is disabled, full_h < 16,
     * the v3 kernel rejected (kernel-level dim check fails), or the
     * llrawproc recon at the cropped+downsampled dim failed. */

    /* Intermediate dim: X reduced by 4, Y identity. */
    const int mid_w = full_w / 4;
    const int mid_h = full_h;
    /* Final output dim: scale=4 in both. */
    const int out_w = full_w / 4;
    const int out_h = full_h / 4;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t mid_rgb_words = mid_pixels * 3u;
    const uint64_t out_rgb_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    uint16_t * full_bayer = x4_source ? x4_fix_buffer : mlv_ensure_thread_u16_buffer(full_pixels);
    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    /* Reuse the rgb_u16 thread-local for the post-debayer mid-RGB buffer.
     * Its capacity grows on demand; oversize for our (mid_w,mid_h) case
     * is harmless. */
    uint16_t * mid_rgb = mlv_ensure_thread_rgb_u16_buffer(mid_rgb_words);
    if (!full_bayer || !mid_bayer || !mid_rgb) return 0;

    /* Step 1: decode raw at full res (no llrawproc). */
    if (!x4_source)
    {
        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, full_bayer))
        {
            memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
            return 0;
        }
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
    }

    /* Step 2: bayer→bayer X-only 4x downsample. */
    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = pl_downsample_bayer_to_bayer_4x_x_only(full_bayer, full_w, full_h,
                                                     mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection("bayer-to-bayer x-only kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    /* Step 3: apply llrawproc subset (HQ recon, dark frame, chroma smooth)
     * on the narrowed buffer. */
    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                           &x4_receipt_flags_snapshot);
    int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                  mid_w, mid_h);
    mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                             &x4_receipt_flags_snapshot);
    if (!llraw_ok)
    {
        mlv_phase4bv2_log_rejection("applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    /* Step 4: debayer narrowed bayer → RGB16 at (mid_w, mid_h). */
    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(mid_rgb, mid_bayer, mid_w, mid_h, threads, bit_shift);

    /* Step 5: RGB Y-only 4x downsample → final (out_w, out_h). */
    int actual_out_w = 0, actual_out_h = 0;
    int rc2 = pl_downsample_rgb_to_rgb_4x_y_only(mid_rgb, mid_w, mid_h,
                                                  outputFrame, &actual_out_w, &actual_out_h, threads);
    if (rc2 != 0 || actual_out_w != out_w || actual_out_h != out_h)
    {
        mlv_phase4bv2_log_rejection("post-recon Y-only RGB downsample rejected");
        return 0;
    }
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    g_mlv_phase4bv2_path_taken = 2;
    return 1;
}

static int mlv_render_scaled_rgb16_v3_full_xy_from_raw(mlvObject_t * video,
                                                       uint64_t frameIndex,
                                                       const uint16_t * decodedRawFrame,
                                                       uint16_t * outputFrame,
                                                       int threads,
                                                       int * out_dn_ms_x100)
{
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 16) * 16;
    if (!decodedRawFrame || full_w % 4 != 0 || eff_h < 16) return 0;

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    const int mid_w = full_w / 4;
    const int mid_h = eff_h / 4;
    const int out_w = full_w / 4;
    const int out_h = full_h / 4;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t mid_rgb_words = mid_pixels * 3u;
    const uint64_t out_rgb_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    uint16_t * mid_rgb = mlv_ensure_thread_rgb_u16_buffer(mid_rgb_words);
    if (!mid_bayer || !mid_rgb) return 0;

    /* Seed the dual-ISO row pattern from the full-res raw before downsampling
     * (mirrors the v4_x8 core); this x4 from-raw path is used by the processed8
     * prefetch worker — the cold-pass corruption source. */
    llrpEnsureDualIsoPatternSeeded(video, (uint16_t *)decodedRawFrame, full_w, full_h);

    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = pl_downsample_bayer_to_bayer_4x(decodedRawFrame, full_w, eff_h,
                                              mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection("v3 bayer-to-bayer 4x kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                  mid_w, mid_h);
    if (!llraw_ok)
    {
        mlv_phase4bv2_log_rejection("v3 applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(outputFrame, mid_bayer, mid_w, mid_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    const size_t row_words = (size_t)out_w * 3u;
    if (mid_h > 0)
    {
        const uint16_t * last_row = outputFrame + (size_t)(mid_h - 1) * row_words;
        for (int y = mid_h; y < out_h; ++y)
        {
            uint16_t * dst = outputFrame + (size_t)y * row_words;
            memcpy(dst, last_row, row_words * sizeof(uint16_t));
        }
    }
    else
    {
        memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
    }

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    if (out_dn_ms_x100)
    {
        *out_dn_ms_x100 = (int)(downsample_ms * 100.0);
    }
    g_mlv_phase4bv2_path_taken = 3;
    return 1;
}

/* Phase 4B-v4: explicit x8 full-XY pre-recon path.
 *
 * This is the early-pipeline version of the manual x8 preview. It reduces
 * Bayer before llrawproc, preserving RGGB and the 4-row Dual ISO phase by
 * sampling complete 4-row groups at source block stride 32. For non-32-aligned
 * heights, the source is cropped down to the nearest 32-row boundary and the
 * missing output rows are filled from the last valid row, matching the v3 x4
 * crop/fill strategy. */
static int mlv_render_scaled_rgb16_v4_x8_full_xy_core(mlvObject_t * video,
                                                      uint64_t frameIndex,
                                                      const uint16_t * decodedRawFrame,
                                                      uint16_t * outputFrame,
                                                      int threads)
{
    if (!video || !outputFrame) return 0;

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 32) * 32;
    if ((full_w % 16) != 0 || eff_h < 32) return 0;

    g_mlv_phase4bv3_y_crop_rows = full_h - eff_h;

    const int mid_w = full_w / 8;
    const int mid_h = eff_h / 8;
    const int out_w = full_w / 8;
    const int out_h = full_h / 8;
    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
    const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
    const uint64_t out_rgb_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    const uint16_t * raw_source = decodedRawFrame;
    uint16_t * full_bayer = NULL;
    if (!raw_source)
    {
        full_bayer = mlv_ensure_thread_u16_buffer(full_pixels);
        if (!full_bayer) return 0;

        const double raw_start = mlv_stage_timing_now();
        if (getMlvRawFrameUint16(video, frameIndex, full_bayer))
        {
            memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
            return 0;
        }
        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
        raw_source = full_bayer;
    }

    /* Seed the dual-ISO row pattern from the full-res raw BEFORE downsampling,
     * so the scaled recon below does not mis-detect it from 8x-downsampled data
     * (the intermittent cold-pass x8 pink). Read-only; no-op unless dual-ISO and
     * the pattern is still unseeded. */
    llrpEnsureDualIsoPatternSeeded(video, (uint16_t *)raw_source, full_w, full_h);

    uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
    if (!mid_bayer) return 0;

    const double downsample_start = mlv_stage_timing_now();
    int actual_mid_w = 0, actual_mid_h = 0;
    int rc = pl_downsample_bayer_to_bayer_8x(raw_source, full_w, eff_h,
                                             mid_bayer, &actual_mid_w, &actual_mid_h, threads);
    if (rc != 0 || actual_mid_w != mid_w || actual_mid_h != mid_h)
    {
        mlv_phase4bv2_log_rejection("v4 x8 bayer-to-bayer kernel rejected");
        return 0;
    }
    const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;

    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
    int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                  mid_w, mid_h);
    if (!llraw_ok)
    {
        mlv_phase4bv2_log_rejection("v4 x8 applyLLRawProcObject_with_dims rejected");
        return 0;
    }
    const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
    g_mlv_last_llrawproc_ms = llrawproc_ms;

    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double debayer_start = mlv_stage_timing_now();
    debayerBasicU16(outputFrame, mid_bayer, mid_w, mid_h, threads, bit_shift);
    const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;

    const size_t row_words = (size_t)out_w * 3u;
    if (mid_h > 0)
    {
        const uint16_t * last_row = outputFrame + (size_t)(mid_h - 1) * row_words;
        for (int y = mid_h; y < out_h; ++y)
        {
            uint16_t * dst = outputFrame + (size_t)y * row_words;
            memcpy(dst, last_row, row_words * sizeof(uint16_t));
        }
    }
    else
    {
        memset(outputFrame, 0, (size_t)out_rgb_words * sizeof(uint16_t));
    }

    g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    g_mlv_phase4bv2_path_taken = 8;
    return 1;
}

static int mlv_render_scaled_rgb16_v4_x8_full_xy(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 uint16_t * outputFrame,
                                                 int threads)
{
    return mlv_render_scaled_rgb16_v4_x8_full_xy_core(video, frameIndex, NULL,
                                                      outputFrame, threads);
}

static int mlv_render_scaled_rgb16_v4_x8_full_xy_from_raw(mlvObject_t * video,
                                                          uint64_t frameIndex,
                                                          const uint16_t * decodedRawFrame,
                                                          uint16_t * outputFrame,
                                                          int threads)
{
    if (!decodedRawFrame) return 0;
    return mlv_render_scaled_rgb16_v4_x8_full_xy_core(video, frameIndex, decodedRawFrame,
                                                      outputFrame, threads);
}

static int mlv_render_scaled_rgb16_from_raw(mlvObject_t * video,
                                            uint64_t frameIndex,
                                            const uint16_t * decodedRawFrame,
                                            uint16_t * outputFrame,
                                            int scaleFactor,
                                            int threads)
{
    if (!video || !decodedRawFrame || !outputFrame || scaleFactor <= 1) return 0;
    if (video->llrawproc)
    {
        video->llrawproc->playback_pre_dualiso_fix_ms = 0.0;
        video->llrawproc->playback_pre_dualiso_fix_completed = 0;
    }

    g_mlv_phase4bv2_path_taken = 0;
    g_mlv_phase4bv3_y_crop_rows = 0;
    g_mlv_phase4bv2_last_fallback_reason = "none";

    const int scale2Aggressive = (scaleFactor == 2 && mlvPlaybackAggressivePreviewMode());
    const int scale2EarlyReductionActive =
        (scaleFactor == 2 && !scale2Aggressive && mlv_phase4bv2_x2_fullres_fix_via_env());
    const int x4FullResFixesActive =
        (scaleFactor == 4 && !mlvPlaybackAggressivePreviewMode() && mlv_phase4bv2_x4_fullres_fix_via_env());
    const int receiptCompatible =
        mlv_phase4bv2_receipt_compatible(video);
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int x2ReceiptBypassActive =
        (scaleFactor == 2 && !mlvPlaybackAggressivePreviewMode() && mlv_phase4bv2_x2_fullres_fix_via_env() && !receiptCompatible);
    const int x8PreviewCompatible = mlv_phase4bv4_x8_preview_compatible(video);

    if ((scaleFactor != 2 || scale2Aggressive || scale2EarlyReductionActive)
     && !mlv_phase4bv2_disabled_via_env()
     && llrpHQDualIso(video)
     && mlv_phase4bv2_quality_allows_pre_recon(video, scaleFactor)
     && (receiptCompatible || scale2EarlyReductionActive || x4FullResFixesActive))
    {
        if (scaleFactor == 2)
        {
            if (x2ReceiptBypassActive)
            {
                const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
                const size_t full_bytes = (size_t)full_pixels * sizeof(uint16_t);
                uint16_t * x2_fix_buffer = mlv_ensure_thread_u16_buffer(full_pixels);
                mlv_phase4bv2_receipt_flag_snapshot_t x2_receipt_flags_snapshot = { 0 };
                if (!x2_fix_buffer)
                {
                    mlv_phase4bv2_log_rejection("x2 full-res fix buffer rejected");
                }
                else
                {
                    const double raw_start = mlv_stage_timing_now();
                    if (getMlvRawFrameUint16(video, frameIndex, x2_fix_buffer))
                    {
                        mlv_phase4bv2_log_rejection("x2 full-res fix raw decode rejected");
                    }
                    else
                    {
                        g_mlv_last_raw_uint16_ms = (mlv_stage_timing_now() - raw_start) * 1000.0;
                        mlv_stage_timing_note_elapsed("raw_uint16", frameIndex, g_mlv_last_raw_uint16_ms);
                        applyLLRawProcObjectPreDualIsoFixes(video, x2_fix_buffer, full_bytes);
                        mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                               &x2_receipt_flags_snapshot);
                        if (mlv_render_scaled_rgb16_x2_full_xy_from_raw(video,
                                                                       frameIndex,
                                                                       x2_fix_buffer,
                                                                       outputFrame,
                                                                       threads))
                        {
                            mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                     &x2_receipt_flags_snapshot);
                            mlv_phase4bv2_set_fallback_reason("none");
                            return 1;
                        }
                        mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                 &x2_receipt_flags_snapshot);
                    }
                }
            }
            if (!mlvPlaybackAggressivePreviewMode()
             && mlv_quarterres_x2_preview_enabled())
            {
                const uint16_t * x2_quarter_source = decodedRawFrame;
                uint16_t * x2_fix_buffer = NULL;
                if (!receiptCompatible && !x2ReceiptBypassActive)
                {
                    const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
                    const size_t full_bytes = (size_t)full_pixels * sizeof(uint16_t);
                    x2_fix_buffer = mlv_ensure_thread_u16_buffer(full_pixels);
                    if (x2_fix_buffer)
                    {
                        memcpy(x2_fix_buffer, decodedRawFrame, full_bytes);
                        applyLLRawProcObjectPreDualIsoFixes(video, x2_fix_buffer, full_bytes);
                        x2_quarter_source = x2_fix_buffer;
                    }
                }
                if (mlv_render_scaled_rgb16_x2_quarter_preview_core(video,
                                                                    frameIndex,
                                                                    x2_quarter_source,
                                                                    outputFrame,
                                                                    threads,
                                                                    /*upscale_to_out*/1,
                                                                    NULL,
                                                                    NULL))
                {
                    return 1;
                }
            }
            if (mlv_render_scaled_rgb16_x2_full_xy(video, frameIndex, outputFrame, threads))
            {
                return 1;
            }
        }
        else if (scaleFactor == 8)
        {
            if (mlv_phase4bv4_x8_disabled_via_env())
            {
                mlv_phase4bv2_log_rejection("MLVAPP_DISABLE_PHASE4BV4_X8 set");
            }
            else if (!x8PreviewCompatible)
            {
                mlv_phase4bv2_log_rejection("x8 preview requires 32-row aligned height");
            }
            else if (mlv_render_scaled_rgb16_v4_x8_full_xy_from_raw(video,
                                                                    frameIndex,
                                                                    decodedRawFrame,
                                                                    outputFrame,
                                                                    threads))
            {
                return 1;
            }
        }
        else if (scaleFactor == 4)
        {
            const uint64_t full_pixels = (uint64_t)full_w * (uint64_t)full_h;
            const size_t full_bytes = (size_t)full_pixels * sizeof(uint16_t);
            const uint16_t * x4_source = decodedRawFrame;
            int x4FixesApplied = receiptCompatible;
            uint16_t * x4_fix_buffer = NULL;
            mlv_phase4bv2_receipt_flag_snapshot_t x4_receipt_flags_snapshot = { 0 };

            if (video->llrawproc)
            {
                video->llrawproc->playback_pre_dualiso_fix_ms = 0.0;
                video->llrawproc->playback_pre_dualiso_fix_completed = 0;
            }

            if (x4FullResFixesActive && !receiptCompatible)
            {
                x4_fix_buffer = mlv_ensure_thread_u16_buffer(full_pixels);
                if (!x4_fix_buffer)
                {
                    mlv_phase4bv2_log_rejection("x4 full-res fix buffer rejected");
                }
                else
                {
                    memcpy(x4_fix_buffer, decodedRawFrame, full_bytes);
                    applyLLRawProcObjectPreDualIsoFixes(video, x4_fix_buffer, full_bytes);
                    x4_source = x4_fix_buffer;
                    mlv_phase4bv2_set_fallback_reason("none");
                    x4FixesApplied = 1;
                }
            }

            if (x4FixesApplied && !mlv_phase4bv3_disabled_via_env() && full_h >= 16)
            {
                int dn_x100 = 0;
                if (x4FullResFixesActive)
                {
                    mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                           &x4_receipt_flags_snapshot);
                }
                if (mlv_render_scaled_rgb16_v3_full_xy_from_raw(video,
                                                                frameIndex,
                                                                x4_source,
                                                                outputFrame,
                                                                threads,
                                                                &dn_x100))
                {
                    if (x4FullResFixesActive)
                    {
                        mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                 &x4_receipt_flags_snapshot);
                    }
                    return 1;
                }
                if (x4FullResFixesActive)
                {
                    mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                             &x4_receipt_flags_snapshot);
                }
            }

            if (x4FixesApplied && full_w > 0 && full_h > 0 && full_w % 4 == 0 && full_h % 4 == 0)
            {
                const int mid_w = full_w / 4;
                const int mid_h = full_h;
                const int out_w = full_w / 4;
                const int out_h = full_h / 4;
                const uint64_t mid_pixels = (uint64_t)mid_w * (uint64_t)mid_h;
                const uint64_t mid_rgb_words = mid_pixels * 3u;
                uint16_t * mid_bayer = mlv_ensure_thread_scaled_bayer_buffer(mid_pixels);
                uint16_t * mid_rgb = mlv_ensure_thread_rgb_u16_buffer(mid_rgb_words);
                if (mid_bayer && mid_rgb)
                {
                    const double downsample_start = mlv_stage_timing_now();
                    int actual_mid_w = 0, actual_mid_h = 0;
                    int rc = pl_downsample_bayer_to_bayer_4x_x_only(x4_source, full_w, full_h,
                                                                     mid_bayer, &actual_mid_w, &actual_mid_h, threads);
                    if (rc == 0 && actual_mid_w == mid_w && actual_mid_h == mid_h)
                    {
                        const double downsample_ms = (mlv_stage_timing_now() - downsample_start) * 1000.0;
                        const double llraw_start = mlv_stage_timing_now();
                        mlv_pipeline_capture_set_current_frame(frameIndex);
                        const size_t mid_bytes = (size_t)mid_pixels * sizeof(uint16_t);
                        if (x4FullResFixesActive)
                        {
                            mlv_phase4bv2_clear_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                   &x4_receipt_flags_snapshot);
                        }
                        int llraw_ok = applyLLRawProcObject_with_dims(video, mid_bayer, mid_bytes,
                                                                      mid_w, mid_h);
                        if (x4FullResFixesActive)
                        {
                            mlv_phase4bv2_restore_coordinate_sensitive_receipt_flags(video->llrawproc,
                                                                                     &x4_receipt_flags_snapshot);
                        }
                        if (llraw_ok)
                        {
                            const double llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
                            mlv_stage_timing_note_elapsed("llrawproc", frameIndex, llrawproc_ms);
                            g_mlv_last_llrawproc_ms = llrawproc_ms;
                            const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
                            const double debayer_start = mlv_stage_timing_now();
                            debayerBasicU16(mid_rgb, mid_bayer, mid_w, mid_h, threads, bit_shift);
                            int actual_out_w = 0, actual_out_h = 0;
                            int rc2 = pl_downsample_rgb_to_rgb_4x_y_only(mid_rgb, mid_w, mid_h,
                                                                          outputFrame, &actual_out_w, &actual_out_h, threads);
                            if (rc2 == 0 && actual_out_w == out_w && actual_out_h == out_h)
                            {
                                const double debayer_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
                                g_mlv_last_debayered_frame_ms = downsample_ms + debayer_ms;
                                mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
                                g_mlv_phase4bv2_path_taken = 2;
                                return 1;
                            }
                        }
                    }
                }
            }
        }
    }
    else if (mlv_phase4bv2_disabled_via_env())
    {
        mlv_phase4bv2_set_fallback_reason_if_none("MLVAPP_DISABLE_PHASE4BV2 set");
    }
    else if (scaleFactor == 2
          && !mlvPlaybackAggressivePreviewMode()
          && !mlv_phase4bv2_x2_fullres_fix_via_env())
    {
        mlv_phase4bv2_set_fallback_reason_if_none("scale=2 uses full-recon post-downsample fallback");
    }
    else if (!llrpHQDualIso(video))
    {
        mlv_phase4bv2_set_fallback_reason_if_none("not HQ Dual ISO; full-recon post-downsample fallback");
    }
    else if (!mlv_phase4bv2_quality_allows_pre_recon(video, scaleFactor))
    {
        mlv_phase4bv2_set_fallback_reason_if_none("quality guard rejected pre-recon path");
    }
    else if (!mlv_phase4bv2_receipt_compatible(video))
    {
        mlv_phase4bv2_set_fallback_reason_if_none("receipt incompatible");
    }
    else if (scaleFactor == 2)
    {
        mlv_phase4bv2_set_fallback_reason_if_none("x2 pre-recon path rejected");
    }

    mlv_phase4bv2_set_fallback_reason_if_none("full-recon post-downsample fallback");

    const int width = (int)getMlvWidth(video);
    const int height = (int)getMlvHeight(video);
    const int out_w = width / scaleFactor;
    const int out_h = height / scaleFactor;
    const uint64_t in_pixels = (uint64_t)width * (uint64_t)height;
    const uint64_t out_words = (uint64_t)out_w * (uint64_t)out_h * 3u;
    uint16_t * unpacked = mlv_ensure_thread_u16_buffer(in_pixels);
    if (!unpacked)
    {
        memset(outputFrame, 0, (size_t)out_words * sizeof(uint16_t));
        return 0;
    }
    memcpy(unpacked, decodedRawFrame, (size_t)in_pixels * sizeof(uint16_t));

    int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    const double llraw_start = mlv_stage_timing_now();
    mlv_pipeline_capture_set_current_frame(frameIndex);
    applyLLRawProcObject(video, unpacked, (size_t)in_pixels * sizeof(uint16_t));
    g_mlv_last_llrawproc_ms = (mlv_stage_timing_now() - llraw_start) * 1000.0;
    mlv_stage_timing_note_elapsed("llrawproc", frameIndex, g_mlv_last_llrawproc_ms);

    const double debayer_start = mlv_stage_timing_now();
    if (scaleFactor == 4)
    {
        pl_downsample_bayer_to_rgb_4x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    else if (scaleFactor == 8)
    {
        pl_downsample_bayer_to_rgb_8x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    else
    {
        pl_downsample_bayer_to_rgb_2x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    return 1;
}

/* Phase 4B: scaled-resolution debayered-RGB16 producer.
 *
 * Phase 4B-v2 path (preferred when compatible): see
 * mlv_render_scaled_rgb16_v2 above — runs the HQ Dual ISO recon on the
 * downsampled bayer (1/16 the pixels at scale=4).
 *
 * Phase 4B-v1 fallback: full LJ92 + full llrawproc + post-downsample. Used
 * when the receipt enables features (focus pixel, bad pixel, vstripes,
 * pattern noise) that require full-res absolute coordinates. The output is
 * already debayered (per-channel block average) so the debayer step is
 * bypassed entirely.
 *
 * Returns 1 on success, 0 on failure (then outputFrame is zeroed). The
 * scaleFactor must be the *effective* scale (caller already ran it
 * through mlv_effective_playback_scale_factor — typically 2 or 4). */
static int mlv_render_scaled_rgb16(mlvObject_t * video,
                                   uint64_t frameIndex,
                                   uint16_t * outputFrame,
                                   int scaleFactor,
                                   int threads)
{
    if (!video || !outputFrame || scaleFactor <= 1) return 0;
    g_mlv_phase4bv2_last_fallback_reason = "none";

    /* Phase 4B-v2 preserves the Dual ISO row pattern, but the pre-LLRawProc
     * Bayer shortcut is visibly harsher on normal clips at x4. Prefer the
     * post-LLRawProc RGB downsample for non-Dual-ISO playback quality. */
    if (llrpHQDualIso(video)
        && mlv_render_scaled_rgb16_v2(video, frameIndex, outputFrame, scaleFactor, threads))
    {
        return 1;
    }
    if (!llrpHQDualIso(video))
    {
        mlv_phase4bv2_set_fallback_reason_if_none("not HQ Dual ISO; full-recon post-downsample fallback");
    }
    else
    {
        mlv_phase4bv2_set_fallback_reason_if_none("full-recon post-downsample fallback");
    }

    const int width  = (int)getMlvWidth(video);
    const int height = (int)getMlvHeight(video);
    const int out_w  = width  / scaleFactor;
    const int out_h  = height / scaleFactor;
    const uint64_t in_pixels = (uint64_t)width * (uint64_t)height;
    const uint64_t out_words = (uint64_t)out_w * (uint64_t)out_h * 3u;

    /* Borrow a thread-local buffer for the full-res Bayer. The TLS
     * buffer grows on demand; once allocated for the full sensor it's
     * reused across calls. */
    uint16_t * unpacked = mlv_ensure_thread_u16_buffer(in_pixels);
    if (!unpacked)
    {
        memset(outputFrame, 0, (size_t)out_words * sizeof(uint16_t));
        return 0;
    }

    int bit_shift = 0;
    const double debayer_start = mlv_stage_timing_now();
    if (getMlvRawFrameProcessedUint16(video, frameIndex, unpacked, &bit_shift))
    {
        memset(outputFrame, 0, (size_t)out_words * sizeof(uint16_t));
        return 0;
    }

    /* Per scope plan: bit_shift widens narrow-bitdepth bayer to 16-bit.
     * For HQ Dual ISO recon, llrawproc has already widened to 16-bit
     * (bit_shift==0). For non-recon paths bit_shift may be non-zero. */
    if (scaleFactor == 4)
    {
        pl_downsample_bayer_to_rgb_4x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    else if (scaleFactor == 8)
    {
        pl_downsample_bayer_to_rgb_8x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    else
    {
        pl_downsample_bayer_to_rgb_2x(unpacked, width, height, outputFrame, bit_shift, threads);
    }
    g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
    mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    return 1;
}

void getMlvRawFrameDebayered(mlvObject_t * video, uint64_t frameIndex, uint16_t * outputFrame)
{
    int width = getMlvWidth(video);
    int height = getMlvHeight(video);
    if (!outputFrame || width <= 0 || height <= 0
        || frameIndex >= getMlvFrames(video)
        || (size_t)width > SIZE_MAX / (size_t)height) {
        return;
    }
    const size_t frame_pixels = (size_t)width * (size_t)height;
    if (frame_pixels > SIZE_MAX / (sizeof(uint16_t) * 3u)) {
        return;
    }
    size_t frame_size = frame_pixels * sizeof(uint16_t) * 3u;
    uint64_t pixels_count = frame_pixels;
    mlv_reset_last_raw_stage_telemetry();
    resetMlvLastDebayerStageMilliseconds();
    mlv_cache_ensure_window(video, frameIndex);
    int copied_from_cache = 0;
    int cache_state = MLV_FRAME_NOT_CACHED;
    pthread_mutex_lock(&video->g_mutexFind);
    int cache_window_active = mlv_frame_in_cache_window(video, frameIndex);
    cache_state = video->cached_frames[frameIndex];
    if (cache_state == MLV_FRAME_NOT_CACHED
        && video->current_cached_frame_active
        && video->current_cached_frame == frameIndex)
    {
        memcpy(outputFrame, video->rgb_raw_current_frame, frame_size);
        copied_from_cache = 1;
    }
    else if (cache_state == MLV_FRAME_IS_CACHED && cache_window_active)
    {
        memcpy(outputFrame,
               video->rgb_raw_frames[mlv_cache_slot_for_frame(video, frameIndex)],
               frame_size);
        copied_from_cache = 1;
    }
    else
    {
        if (cache_state == MLV_FRAME_IS_CACHED)
        {
            video->cached_frames[frameIndex] = MLV_FRAME_NOT_CACHED;
            cache_state = MLV_FRAME_NOT_CACHED;
        }
        if (cache_state == MLV_FRAME_NOT_CACHED && cache_window_active)
            video->cache_next = frameIndex;
    }
    pthread_mutex_unlock(&video->g_mutexFind);
    if (copied_from_cache) return;

    if (doesMlvAlwaysUseAmaze(video) && isMlvObjectCaching(video))
    {
        while (isMlvObjectCaching(video))
        {
            pthread_mutex_lock(&video->g_mutexFind);
            cache_window_active = mlv_frame_in_cache_window(video, frameIndex);
            cache_state = video->cached_frames[frameIndex];
            if (cache_state == MLV_FRAME_IS_CACHED && cache_window_active)
            {
                memcpy(outputFrame,
                       video->rgb_raw_frames[mlv_cache_slot_for_frame(video, frameIndex)],
                       frame_size);
                copied_from_cache = 1;
            }
            pthread_mutex_unlock(&video->g_mutexFind);
            if (copied_from_cache) return;
            usleep(100);
        }
    }

    float * raw_frame = mlv_ensure_float_buffer(&video->raw_debayer_temp_frame,
                                                &video->raw_debayer_temp_frame_pixels,
                                                pixels_count);
    uint16_t * current_frame = mlv_ensure_u16_buffer(&video->rgb_raw_current_frame,
                                                     &video->rgb_raw_current_frame_words,
                                                     pixels_count * 3);
    if (!raw_frame || !current_frame)
    {
        memset(outputFrame, 0, frame_size);
        pthread_mutex_lock(&video->g_mutexFind);
        video->current_cached_frame_active = 0;
        pthread_mutex_unlock(&video->g_mutexFind);
        return;
    }

    get_mlv_raw_frame_debayered(video, frameIndex, raw_frame, current_frame, doesMlvAlwaysUseAmaze(video));
    pthread_mutex_lock(&video->g_mutexFind);
    memcpy(outputFrame, video->rgb_raw_current_frame, frame_size);
    video->current_cached_frame_active = 1;
    video->current_cached_frame = frameIndex;
    pthread_mutex_unlock(&video->g_mutexFind);
}

static void mlv_compute_desired_processing_bw_levels(mlvObject_t * video,
                                                     float * desired_black_level,
                                                     int * desired_white_level,
                                                     int * desired_bit_depth,
                                                     int * expected_black_level,
                                                     int * expected_white_level)
{
    *desired_bit_depth = llrpHQDualIso(video)
        ? video->llrawproc->dng_bit_depth
        : getMlvBitdepth(video);
    *desired_black_level = llrpHQDualIso(video)
        ? (float)video->llrawproc->dng_black_level
        : (float)getMlvBlackLevel(video);
    *desired_white_level = llrpHQDualIso(video)
        ? video->llrawproc->dng_white_level
        : getMlvWhiteLevel(video);
    const int bits_shift = 16 - *desired_bit_depth;
    *expected_black_level =
        (*desired_black_level > 0.0f)
            ? (int)(*desired_black_level * pow(2.0, bits_shift))
            : 0;
    *expected_white_level =
        (int)((double)(*desired_white_level << bits_shift) * 0.993);
}

static void mlv_sync_processing_black_white_levels(mlvObject_t * video)
{
    float desired_black_level = 0.0f;
    int desired_white_level = 0;
    int desired_bit_depth = 0;
    int expected_black_level = 0;
    int expected_white_level = 0;
    mlv_compute_desired_processing_bw_levels(video,
                                             &desired_black_level,
                                             &desired_white_level,
                                             &desired_bit_depth,
                                             &expected_black_level,
                                             &expected_white_level);

    if ((int)video->processing->black_level != expected_black_level
     || video->processing->white_level != expected_white_level)
    {
        processingSetBlackAndWhiteLevel(video->processing,
                                        desired_black_level,
                                        desired_white_level,
                                        desired_bit_depth);
    }
}

/* The fast/subset playback-preview path renders through the GPU-preview
 * config (GpuPreviewProcessing) instead of the classic processed-frame cores,
 * so it never reaches the per-frame mlv_sync_processing_black_white_levels()
 * those cores run. For an HQ dual-ISO clip whose recon rescales the data
 * range (e.g. restricted-range lossless: RAWI white ~6000 scaled to a
 * ~63000-domain), the processing object then keeps its clip-open pre-scale
 * levels and the subset renders the recon-scaled data several times too
 * bright with clipped highlights. These two entry points let the subset path
 * mirror the cores' sync: check first (cheap, no mutation), then sync under
 * the caller's render-idle guard. */
int mlvProcessingDualIsoBlackWhiteLevelsOutOfSync(mlvObject_t * video)
{
    if (!video || !video->processing || !video->llrawproc) return 0;
    float desired_black_level = 0.0f;
    int desired_white_level = 0;
    int desired_bit_depth = 0;
    int expected_black_level = 0;
    int expected_white_level = 0;
    mlv_compute_desired_processing_bw_levels(video,
                                             &desired_black_level,
                                             &desired_white_level,
                                             &desired_bit_depth,
                                             &expected_black_level,
                                             &expected_white_level);
    return ((int)video->processing->black_level != expected_black_level)
        || (video->processing->white_level != expected_white_level);
}

void mlvSyncProcessingDualIsoBlackWhiteLevels(mlvObject_t * video)
{
    if (!video || !video->processing || !video->llrawproc) return;
    mlv_sync_processing_black_white_levels(video);
}

static int mlv_can_use_direct_processed_frame8_path(mlvObject_t * video)
{
    return video
        && video->processing
        && processingCanUseDirect8BitOutput(video->processing);
}

/* Round-4 item 2 (x1 scope guard): the direct8 fused path renders at FULL
 * resolution at scale 1, while the x1 reduced proxy processes 1/4 (half) or
 * 1/16 (quarter) of the pixels. When the preview unlock let direct8 preempt
 * the proxy, x1 dropped 13 -> 9 fps on all three M16 clips (processed16
 * 48-52ms -> 84-90ms): the fused-pipeline win cannot beat a 4x pixel
 * reduction. Direct8 keeps its dispatch priority at x1 only when the reduced
 * proxy would NOT engage (preview off, proxy disabled / level Full, or the
 * clip ineligible for the reduced core - mirrors the eligibility checks at
 * the top of mlv_render_scaled_rgb16_x1_half_preview_core). */
static int mlv_x1_reduced_proxy_would_engage(mlvObject_t * video)
{
    if (!processingPlaybackPreviewModeEnabled()) return 0;
    if (!mlv_halfres_x1_preview_enabled()) return 0;
    if (!video || !llrpHQDualIso(video)) return 0;
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int eff_h = (full_h / 16) * 16;
    if ((full_w % 4) != 0 || eff_h < 16) return 0;
    return 1;
}

/* Round-4 item 2 scope: preview direct8 wins only where its INPUT side stays
 * on a cheap path. Two measured loss regimes on the M16 trio:
 *   (a) x1 with the reduced proxy available - the proxy processes 1/4-1/16
 *       of the pixels (guard above);
 *   (b) dual-ISO clips outside HQ recon (the mean23 preview-decode regime,
 *       Playback quality "preview"): mlv_render_scaled_rgb16 cannot take the
 *       bayer-downsample shortcut there and falls back to full recon + full
 *       debayer (llrawproc ~30ms + debayer ~32ms measured), while the
 *       indirect dispatcher still has its cheap mean23-aware scaled cores -
 *       x2 sagged 14.9 -> 11.4 when direct8 preempted them.
 * Pause/export calls (preview mode off) are not a dispatch decision here. */
static int mlv_preview_direct8_input_is_cheap(mlvObject_t * video, int normalizedScale)
{
    if (!processingPlaybackPreviewModeEnabled())
    {
        return 1;
    }
    if (normalizedScale == 1 && mlv_x1_reduced_proxy_would_engage(video))
    {
        return 0;
    }
    if (video && video->llrawproc
        && video->llrawproc->diso_validity
        && video->llrawproc->dual_iso != 0
        && !llrpHQDualIso(video))
    {
        return 0;
    }
    return 1;
}

/* Round-4 item 2: never let the direct8 kernel write the caller's buffer in
 * place across the full render duration. The playback caller hands the LIVE
 * presented frame buffer down here; the row-parallel kernel writes it over
 * ~10-20ms, and out-of-band paints (window captures, compositor reads)
 * sample a torn mix of old and new row blocks - VIEWED x2 filmstrips showed
 * multi-band temporal tearing on every capture, with or without the prefetch
 * worker. The indirect path only exposes its ~2ms packdown window; render
 * into TLS scratch and publish with one memcpy to match that window. The
 * prefetch worker already passes this thread's TLS u8 slot as outputFrame -
 * same pointer, so the publish copy self-elides there. */
static void mlv_apply_processing_object8_published(processingObject_t * processing,
                                                   int out_w,
                                                   int out_h,
                                                   uint16_t * unprocessed_frame,
                                                   uint8_t * outputFrame,
                                                   uint64_t rgb_frame_size,
                                                   int threads,
                                                   uint64_t frameIndex)
{
    uint8_t * scratch = mlv_ensure_thread_u8_buffer(rgb_frame_size);
    uint8_t * target = scratch ? scratch : outputFrame;
    applyProcessingObject8(processing,
                           out_w,
                           out_h,
                           unprocessed_frame,
                           target,
                           threads,
                           1,
                           frameIndex);
    if (target != outputFrame)
    {
        memcpy(outputFrame, target, (size_t)rgb_frame_size);
    }
}

static int mlv_render_processed_frame8_direct_with_processing(mlvObject_t * video,
                                                              processingObject_t * processing,
                                                              int syncProcessingLevels,
                                                              uint64_t frameIndex,
                                                              uint8_t * outputFrame,
                                                              int threads,
                                                              int scaleFactor,
                                                              int recordTelemetry)
{
    const int eff_scale = mlv_normalize_playback_scale_factor(scaleFactor);
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = (eff_scale > 1) ? (full_w / eff_scale) : full_w;
    const int out_h = (eff_scale > 1) ? (full_h / eff_scale) : full_h;

    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3;
    const uint64_t pixels_count = (uint64_t)full_w * (uint64_t)full_h;
    float * raw_frame = mlv_ensure_thread_float_buffer(pixels_count);
    /* Pre-grow the TLS rgb_u16 slot to full-res before capturing: the
     * scaled decode's internals ensure mid-res intermediates from the SAME
     * thread-local slot, and a growth realloc would move the buffer out
     * from under this captured pointer (silent use-after-free; on a fresh
     * worker thread the slot starts at zero capacity, so scaled renders
     * were exposed). Full-res RGB bounds every intermediate. */
    const uint64_t rgb_buf_words = (uint64_t)full_w * (uint64_t)full_h * 3u;
    uint16_t * unprocessed_frame = mlv_ensure_thread_scaled_input_buffer(rgb_buf_words);
    if (!raw_frame || !unprocessed_frame || !processing)
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    /* applyProcessingObject8 is a silent no-op when the processing state is
     * direct8-incompatible; reporting success with an unwritten outputFrame
     * lets callers cache and present stale pixels (the processed8 prefetch
     * stored the same frozen frame under every advancing frame index). Fail
     * closed instead. */
    if (!processingCanUseDirect8BitOutput(processing))
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    const double processed16_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    const double debayer_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    if (eff_scale > 1)
    {
        /* Phase 4B: fused downsample-and-debayer at scale > 1. The
         * unprocessed_frame buffer is filled with scaled RGB16 directly;
         * the debayer step is bypassed entirely. */
        if (!mlv_render_scaled_rgb16(video, frameIndex, unprocessed_frame, eff_scale, threads))
        {
            memset(outputFrame, 0, (size_t)rgb_frame_size);
            return 0;
        }
    }
    else
    {
        get_mlv_raw_frame_debayered(video,
                                    frameIndex,
                                    raw_frame,
                                    unprocessed_frame,
                                    doesMlvAlwaysUseAmaze(video));
    }
    if (recordTelemetry)
    {
        g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
        mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    }

    /* SDBG_direct8_in capture: the scaled RGB16 fed into the direct8
     * processing stage. Inert when MLVAPP_PIPELINE_CAPTURE_DIR is unset.
     * The stage name encodes the caller (foreground render vs processed8
     * prefetch worker) so concurrent captures of the same frame don't
     * collide on the output filename. */
    if (mlv_pipeline_capture_should_capture_frame(frameIndex))
    {
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = recordTelemetry ? "SDBG_direct8_in_fg" : "SDBG_direct8_in_pf";
        meta.format = MLV_PIPELINE_FORMAT_UINT16_RGB;
        meta.format_label = "uint16_rgb_direct8_input";
        meta.width = out_w;
        meta.height = out_h;
        meta.bytes_per_line = out_w * 3 * (int)sizeof(uint16_t);
        meta.bytes_per_pixel = 3 * (int)sizeof(uint16_t);
        meta.channels = 3;
        meta.bit_depth = 16;
        meta.scaler = (eff_scale > 1) ? "playback_downsample" : "none";
        meta.path_label = recordTelemetry ? "foreground" : "prefetch";
        mlv_pipeline_capture(frameIndex, unprocessed_frame, &meta);
    }

    if (syncProcessingLevels)
    {
        mlv_sync_processing_black_white_levels(video);
    }

    const double processing_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    mlv_apply_processing_object8_published(processing,
                                           out_w,
                                           out_h,
                                           unprocessed_frame,
                                           outputFrame,
                                           rgb_frame_size,
                                           threads,
                                           frameIndex);

    /* SDBG_direct8_out capture: the direct8 8-bit output right after the
     * processing stage. Inert when MLVAPP_PIPELINE_CAPTURE_DIR is unset.
     * If _in advances across frames while _out stays frozen, the direct8
     * stage did not write outputFrame (path_label records the gate). */
    if (mlv_pipeline_capture_should_capture_frame(frameIndex))
    {
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = recordTelemetry ? "SDBG_direct8_out_fg" : "SDBG_direct8_out_pf";
        meta.format = MLV_PIPELINE_FORMAT_UINT8_RGB;
        meta.format_label = "uint8_rgb_direct8_output";
        meta.width = out_w;
        meta.height = out_h;
        meta.bytes_per_line = out_w * 3;
        meta.bytes_per_pixel = 3;
        meta.channels = 3;
        meta.bit_depth = 8;
        meta.scaler = (eff_scale > 1) ? "playback_downsample" : "none";
        meta.path_label = processingCanUseDirect8BitOutput(processing)
            ? "direct8_gate_pass"
            : "direct8_gate_fail";
        mlv_pipeline_capture(frameIndex, outputFrame, &meta);
    }
    if (recordTelemetry)
    {
        g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);
        g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - processed16_start) * 1000.0;
        g_mlv_last_processed16_for_8bit_ms = g_mlv_last_processed16_total_ms;
        g_mlv_last_processed16_to_8bit_ms = 0.0;
        g_mlv_last_processed8_direct_path_active = 1;
        mlv_stage_timing_note_elapsed("processed16_total", frameIndex, g_mlv_last_processed16_total_ms);
        mlv_stage_timing_note_elapsed("processed16_for_8bit", frameIndex, g_mlv_last_processed16_for_8bit_ms);
        mlv_stage_timing_note_elapsed("processed16_to_8bit", frameIndex, g_mlv_last_processed16_to_8bit_ms);
    }

    return 1;
}

/* Indirect (processed16 -> 8) render for the prefetch worker. Mirrors the
 * foreground fallback in getMlvProcessedFrame8_with_scale: scaled RGB16
 * decode/recon, full 16-bit applyProcessingObject on the worker's private
 * processing copy, then the same >>8 packdown. Worker-thread-safe by
 * construction: TLS buffers only, never video->rgb_processed_current_frame.
 * Scale 1 is rejected (a full-res prefetch render costs about a foreground
 * frame, and the standard prefetch gate keeps x1 off anyway). Fails closed:
 * returns 0 unless outputFrame was fully written. */
static int mlv_render_processed_frame8_indirect_with_processing(mlvObject_t * video,
                                                                processingObject_t * processing,
                                                                uint64_t frameIndex,
                                                                uint8_t * outputFrame,
                                                                int threads,
                                                                int scaleFactor)
{
    if (!video || !processing || !outputFrame)
    {
        return 0;
    }

    const int eff_scale = mlv_normalize_playback_scale_factor(scaleFactor);
    if (eff_scale <= 1)
    {
        return 0;
    }

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = full_w / eff_scale;
    const int out_h = full_h / eff_scale;
    if (out_w <= 0 || out_h <= 0)
    {
        return 0;
    }

    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3u;
    /* Pre-grow the TLS slot to full-res RGB before capturing the pointer:
     * the scaled decode's internals ensure mid-res intermediates from the
     * SAME thread-local slot, and a growth realloc would move the buffer
     * out from under the captured pointer (silent use-after-free until the
     * heap reuses the block). Full-res RGB is the supremum of every
     * intermediate, so one capture stays valid for the whole render. */
    const uint64_t rgb_full_words = (uint64_t)full_w * (uint64_t)full_h * 3u;
    uint16_t * unprocessed_frame = mlv_ensure_thread_scaled_input_buffer(rgb_full_words);
    if (!unprocessed_frame)
    {
        return 0;
    }

    if (!mlv_render_scaled_rgb16(video, frameIndex, unprocessed_frame, eff_scale, threads))
    {
        return 0;
    }

    /* The scaled decode used the bayer-side TLS u16 slot as scratch; that
     * data is dead once the decode returns, so the slot is reused as the
     * processed16 output. Input and output must stay distinct buffers (the
     * processing stages read source neighborhoods while writing). */
    uint16_t * processed_frame = mlv_ensure_thread_u16_buffer(rgb_frame_size);
    if (!processed_frame)
    {
        return 0;
    }

    applyProcessingObject(processing,
                          out_w,
                          out_h,
                          unprocessed_frame,
                          processed_frame,
                          threads,
                          1,
                          frameIndex);

    const size_t row_words = (size_t)out_w * 3u;
    #pragma omp parallel for schedule(static)
    for (int y = 0; y < out_h; ++y)
    {
        const uint16_t * __restrict src_row = processed_frame + ((size_t)y * row_words);
        uint8_t * __restrict dst_row = outputFrame + ((size_t)y * row_words);
        for (size_t x = 0; x < row_words; ++x)
        {
            dst_row[x] = (uint8_t)(src_row[x] >> 8);
        }
    }

    /* SDBG_indirect8_out capture: worker-side indirect output (inert unless
     * MLVAPP_PIPELINE_CAPTURE_DIR is set) so hit-content forensics work on
     * this path exactly like the direct8 one. */
    if (mlv_pipeline_capture_should_capture_frame(frameIndex))
    {
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = "SDBG_indirect8_out_pf";
        meta.format = MLV_PIPELINE_FORMAT_UINT8_RGB;
        meta.format_label = "uint8_rgb_processed16_packdown";
        meta.width = out_w;
        meta.height = out_h;
        meta.bytes_per_line = out_w * 3;
        meta.bytes_per_pixel = 3;
        meta.channels = 3;
        meta.bit_depth = 8;
        meta.scaler = "playback_downsample";
        meta.path_label = "prefetch_indirect8";
        mlv_pipeline_capture(frameIndex, outputFrame, &meta);
    }

    return 1;
}

static int mlv_render_processed_frame8_direct_with_processing_from_raw(mlvObject_t * video,
                                                                       processingObject_t * processing,
                                                                       int syncProcessingLevels,
                                                                       uint64_t frameIndex,
                                                                       const uint16_t * decodedRawFrame,
                                                                       uint8_t * outputFrame,
                                                                       int threads,
                                                                       int scaleFactor,
                                                                       int recordTelemetry)
{
    if (!video || !decodedRawFrame || !outputFrame || !processing) return 0;

    const int eff_scale = mlv_normalize_playback_scale_factor(scaleFactor);
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = (eff_scale > 1) ? (full_w / eff_scale) : full_w;
    const int out_h = (eff_scale > 1) ? (full_h / eff_scale) : full_h;

    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3;
    /* Pre-grow to full-res before capturing (same TLS realloc hazard as the
     * sibling helpers: internals grow this slot for mid-res intermediates). */
    const uint64_t rgb_buf_words = (uint64_t)full_w * (uint64_t)full_h * 3u;
    uint16_t * unprocessed_frame = mlv_ensure_thread_scaled_input_buffer(rgb_buf_words);
    if (!decodedRawFrame || !unprocessed_frame || !processing || eff_scale <= 1)
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    /* Same direct8 gate as mlv_render_processed_frame8_direct_with_processing:
     * never report success without writing outputFrame. */
    if (!processingCanUseDirect8BitOutput(processing))
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    const double processed16_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    const double debayer_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    if (!mlv_render_scaled_rgb16_from_raw(video,
                                          frameIndex,
                                          decodedRawFrame,
                                          unprocessed_frame,
                                          eff_scale,
                                          threads))
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }
    if (recordTelemetry)
    {
        g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
        mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    }

    if (syncProcessingLevels)
    {
        mlv_sync_processing_black_white_levels(video);
    }

    const double processing_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    mlv_apply_processing_object8_published(processing,
                                           out_w,
                                           out_h,
                                           unprocessed_frame,
                                           outputFrame,
                                           rgb_frame_size,
                                           threads,
                                           frameIndex);
    if (recordTelemetry)
    {
        g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);
        g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - processed16_start) * 1000.0;
        g_mlv_last_processed16_for_8bit_ms = g_mlv_last_processed16_total_ms;
        g_mlv_last_processed16_to_8bit_ms = 0.0;
        g_mlv_last_processed8_direct_path_active = 1;
        mlv_stage_timing_note_elapsed("processed16_total", frameIndex, g_mlv_last_processed16_total_ms);
        mlv_stage_timing_note_elapsed("processed16_for_8bit", frameIndex, g_mlv_last_processed16_for_8bit_ms);
        mlv_stage_timing_note_elapsed("processed16_to_8bit", frameIndex, g_mlv_last_processed16_to_8bit_ms);
    }

    return 1;
}

static int mlv_render_processed_frame8_direct_with_processing_from_reconned_raw(mlvObject_t * video,
                                                                                processingObject_t * processing,
                                                                                int syncProcessingLevels,
                                                                                uint64_t frameIndex,
                                                                                const uint16_t * reconnedRawFrame,
                                                                                uint8_t * outputFrame,
                                                                                int threads,
                                                                                int scaleFactor,
                                                                                int allowScale1StateDebayer,
                                                                                int recordTelemetry)
{
    if (!video || !reconnedRawFrame || !outputFrame || !processing) return 0;

    const int eff_scale = mlv_normalize_playback_scale_factor(scaleFactor);
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = (eff_scale > 1) ? (full_w / eff_scale) : full_w;
    const int out_h = (eff_scale > 1) ? (full_h / eff_scale) : full_h;

    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3;
    const uint64_t rgb_buf_words = (eff_scale > 1)
        ? rgb_frame_size
        : (uint64_t)full_w * (uint64_t)full_h * 3u;
    uint16_t * unprocessed_frame = mlv_ensure_thread_scaled_input_buffer(rgb_buf_words);
    if (!unprocessed_frame || (eff_scale <= 1 && !allowScale1StateDebayer))
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    /* Same direct8 gate as mlv_render_processed_frame8_direct_with_processing:
     * never report success without writing outputFrame. */
    if (!processingCanUseDirect8BitOutput(processing))
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }

    const double processed16_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    const double debayer_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    const int bit_shift = llrpHQDualIso(video) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);
    if (eff_scale == 1)
    {
        /* CUDA no-readback x1 presents from the retained reconstructed Bayer
         * on the GPU. This render is only the fresh Look Assist frame-state
         * producer, so avoid the full CPU AMaZE fallback that otherwise
         * dominates the path; the displayed pixels still come from the CUDA
         * AMaZE/device-Bayer presenter. */
        debayerBasicU16(unprocessed_frame, reconnedRawFrame, full_w, full_h,
                        threads, bit_shift);
    }
    else if (eff_scale == 4)
    {
        pl_downsample_bayer_to_rgb_4x(reconnedRawFrame, full_w, full_h,
                                      unprocessed_frame, bit_shift, threads);
    }
    else if (eff_scale == 8)
    {
        pl_downsample_bayer_to_rgb_8x(reconnedRawFrame, full_w, full_h,
                                      unprocessed_frame, bit_shift, threads);
    }
    else if (eff_scale == 2)
    {
        pl_downsample_bayer_to_rgb_2x(reconnedRawFrame, full_w, full_h,
                                      unprocessed_frame, bit_shift, threads);
    }
    else
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size);
        return 0;
    }
    if (recordTelemetry)
    {
        g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
        mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
    }

    if (syncProcessingLevels)
    {
        mlv_sync_processing_black_white_levels(video);
    }

    const double processing_start = recordTelemetry ? mlv_stage_timing_now() : 0.0;
    mlv_apply_processing_object8_published(processing,
                                           out_w,
                                           out_h,
                                           unprocessed_frame,
                                           outputFrame,
                                           rgb_frame_size,
                                           threads,
                                           frameIndex);
    if (recordTelemetry)
    {
        g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);
        g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - processed16_start) * 1000.0;
        g_mlv_last_processed16_for_8bit_ms = g_mlv_last_processed16_total_ms;
        g_mlv_last_processed16_to_8bit_ms = 0.0;
        g_mlv_last_processed8_direct_path_active = 1;
        mlv_stage_timing_note_elapsed("processed16_total", frameIndex, g_mlv_last_processed16_total_ms);
        mlv_stage_timing_note_elapsed("processed16_for_8bit", frameIndex, g_mlv_last_processed16_for_8bit_ms);
        mlv_stage_timing_note_elapsed("processed16_to_8bit", frameIndex, g_mlv_last_processed16_to_8bit_ms);
    }

    return 1;
}

/* Get a processed frame in 16 bit, only use more than one thread for preview as
 * it may have minor artifacts (though I haven't found them yet) */
static void getMlvProcessedFrame16_with_scale(mlvObject_t * video,
                                              uint64_t frameIndex,
                                              uint16_t * outputFrame,
                                              int threads,
                                              int scaleFactor,
                                              int store_processed16_cache,
                                              uint64_t precomputed_signature)
{
    const double total_start = mlv_stage_timing_now();
    g_mlv_phase4bv2_path_taken = 0;
    g_mlv_phase4bv3_y_crop_rows = 0;
    g_mlv_phase4bv2_last_fallback_reason = "none";
    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_debayered_frame_ms = 0.0;
    g_mlv_last_processing_ms = 0.0;
    g_mlv_last_processed16_total_ms = 0.0;
    g_mlv_last_processed16_cache_store_ms = 0.0;
    g_mlv_last_processed8_direct_path_active = 0;
    g_mlv_last_processed8_cache_hit = 0;
    g_mlv_last_processed8_cache_hit_scale_factor = 0;

    /* Phase 4B: resolve effective scale (rejects scales that don't divide
     * the sensor evenly). The cache key uses the *effective* scale so a
     * request that gets clamped doesn't collide with a different request
     * that lands at the same scale. */
    const int normalizedScale = mlv_effective_playback_scale_factor(video, scaleFactor);
    if (video)
    {
        video->playback_scale_factor_active = normalizedScale;
    }

    /* Phase 4B: width/height are the OUTPUT (scaled) dimensions for the
     * downstream processing call and the cache. The full sensor
     * dimensions are still required by the downsample kernel; we capture
     * those in full_w / full_h. */
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    int width  = (normalizedScale > 1) ? (full_w / normalizedScale) : full_w;
    int height = (normalizedScale > 1) ? (full_h / normalizedScale) : full_h;

    /* Size of RAW frame */
    uint64_t rgb_frame_size = (uint64_t)height * width * 3;
    uint64_t requested_signature = 0;
    uint64_t entry_state_fingerprint = 0;
    if (store_processed16_cache)
    {
        /* Round-4 item R4-1: the playback 8-bit entry already computed this
         * exact signature against the same pre-render state; recomputing the
         * ~1.1MB state hash here doubled the per-frame hashing cost on the
         * indirect path. 0 means "no precomputed signature available". */
        requested_signature = precomputed_signature
            ? precomputed_signature
            : mlv_processed_frame_signature_with_scale(video,
                                                       frameIndex,
                                                       normalizedScale);
        entry_state_fingerprint = mlv_render_mutable_state_fingerprint(video);

        if (video->current_processed_frame_active
            && video->current_processed_frame == frameIndex
            && video->current_processed_frame_threads == threads
            && video->current_processed_frame_signature == requested_signature
            && video->rgb_processed_current_frame)
        {
            if (outputFrame != video->rgb_processed_current_frame)
            {
                memcpy(outputFrame, video->rgb_processed_current_frame, (size_t)rgb_frame_size * sizeof(uint16_t));
            }

            g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
            mlv_stage_timing_note("processed16_total", frameIndex, total_start);
            return;
        }

        int cached_slot = mlv_find_processed_frame_16bit_cache_slot_with_scale(video,
                                                                               frameIndex,
                                                                               threads,
                                                                               requested_signature,
                                                                               normalizedScale);
        if (cached_slot >= 0)
        {
            uint16_t * cached_frame = mlv_processed_frame_16bit_cache_slot(video, (uint32_t)cached_slot, rgb_frame_size);
            if (cached_frame)
            {
                if (outputFrame != cached_frame)
                {
                    memcpy(outputFrame, cached_frame, (size_t)rgb_frame_size * sizeof(uint16_t));
                }

                uint16_t * exact_cache = mlv_ensure_u16_buffer(&video->rgb_processed_current_frame,
                                                               &video->rgb_processed_current_frame_words,
                                                               rgb_frame_size);
                if (exact_cache)
                {
                    if (exact_cache != cached_frame)
                    {
                        memcpy(exact_cache, cached_frame, (size_t)rgb_frame_size * sizeof(uint16_t));
                    }
                    video->current_processed_frame_active = 1;
                    video->current_processed_frame = frameIndex;
                    video->current_processed_frame_threads = threads;
                    video->current_processed_frame_signature = requested_signature;
                }
                else
                {
                    video->current_processed_frame_active = 0;
                    video->current_processed_frame_signature = 0;
                }

                g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
                mlv_stage_timing_note("processed16_total", frameIndex, total_start);
                return;
            }

            mlv_reset_processed_frame_16bit_cache(video);
        }
    }

    /* Unprocessed debayered frame (RGB) */
    uint16_t * unprocessed_frame = mlv_ensure_u16_buffer(&video->rgb_processed_temp_frame,
                                                         &video->rgb_processed_temp_frame_words,
                                                         rgb_frame_size);
    if (!unprocessed_frame)
    {
        memset(outputFrame, 0, (size_t)rgb_frame_size * sizeof(uint16_t));
        video->current_processed_frame_active = 0;
        g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
        mlv_stage_timing_note("processed16_total", frameIndex, total_start);
        return;
    }

    /* Phase 4B: at scale > 1, route through the fused
     * downsample-and-debayer that produces RGB16 directly at scaled
     * dimensions. The debayer step is skipped entirely on this path. */
    if (normalizedScale > 1)
    {
        /* Round-3 item 5: at x2 Sharp playback preview, process at the
         * quarter render's native dims (effective scale 4 = the proven SH
         * x4 lane) and upscale the PROCESSED result - the same lever that
         * took x1 from ~9 to ~17 fps. Fail-open: any rejection falls
         * through to the unchanged full dispatcher below. */
        if (normalizedScale == 2
         && processingPlaybackPreviewModeEnabled()
         && !mlvPlaybackAggressivePreviewMode()
         && mlv_quarterres_x2_preview_enabled()
         && mlv_quarterres_x2_processing_enabled()
         && llrpHQDualIso(video))
        {
            int q_w = 0, q_h = 0;
            if (mlv_render_scaled_rgb16_x2_quarter_preview_core(video,
                                                                frameIndex,
                                                                NULL,
                                                                unprocessed_frame,
                                                                threads,
                                                                /*upscale_to_out*/0,
                                                                &q_w,
                                                                &q_h))
            {
                const uint64_t rgb_out_words =
                    (uint64_t)width * (uint64_t)height * 3u;
                uint16_t * processed_q = mlv_ensure_thread_rgb_u16_buffer(rgb_out_words);
                if (processed_q)
                {
                    mlv_sync_processing_black_white_levels(video);
                    const double processing_start = mlv_stage_timing_now();
                    const int previous_preview_scale_factor =
                        processingPlaybackPreviewScaleFactor();
                    processingSetPlaybackPreviewScaleFactor(4);
                    applyProcessingObject( video->processing,
                                           q_w, q_h,
                                           unprocessed_frame,
                                           processed_q,
                                           threads, 1, frameIndex );
                    processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
                    mlv_rgb_u16_upscale_to_size(processed_q, q_w, q_h,
                                                outputFrame, width, height, threads);
                    g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
                    mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);
                    goto processed16_store;
                }
            }
        }
        if (!mlv_render_scaled_rgb16(video, frameIndex, unprocessed_frame, normalizedScale, threads))
        {
            memset(outputFrame, 0, (size_t)rgb_frame_size * sizeof(uint16_t));
            video->current_processed_frame_active = 0;
            g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
            mlv_stage_timing_note("processed16_total", frameIndex, total_start);
            return;
        }
    }
    else
    {
        const int halfProcessingWanted =
            processingPlaybackPreviewModeEnabled()
            && mlv_halfres_x1_preview_enabled()
            && mlv_halfres_x1_processing_enabled();
        /* Round-3 item 3: the UI Quarter level requests two halvings; the
         * core degrades to half when the clip dims reject the 4x kernel. */
        const int proxyHalvings = (mlvPlaybackProxyLevel() == 2) ? 2 : 1;
        int mid_w = 0, mid_h = 0;
        int usedHalvings = 0;
        int reducedRenderOk = 0;
        if (halfProcessingWanted)
        {
            for (int h = proxyHalvings; h >= 1 && !reducedRenderOk; --h)
            {
                reducedRenderOk = mlv_render_scaled_rgb16_x1_half_preview_core(
                    video, frameIndex, NULL, unprocessed_frame, threads,
                    /*upscale_to_full*/0, h, &mid_w, &mid_h);
                if (reducedRenderOk) usedHalvings = h;
            }
        }
        if (reducedRenderOk)
        {
            /* Round-3 item 2: the proxy left the HALF-RES image in
             * unprocessed_frame. Process at mid dimensions into the TLS rgb
             * slot (pre-grown to full-res per the b90a954f capture rule;
             * distinct from both unprocessed_frame and outputFrame), then
             * upscale the PROCESSED result to the full-size outputFrame.
             * The processing+upscale pair replaces the common full-res
             * applyProcessingObject below; both legs are timed as
             * "processing" so A/Bs compare like for like. */
            const uint64_t rgb_full_words =
                (uint64_t)full_w * (uint64_t)full_h * 3u;
            uint16_t * processed_mid = mlv_ensure_thread_rgb_u16_buffer(rgb_full_words);
            if (processed_mid)
            {
                /* Round-3 item 4 root cause of the item-3 dark bias: the
                 * shadows/highlights machinery selects its internal
                 * resolution path from processingPlaybackPreviewScaleFactor
                 * and assumes the input matches that scale
                 * (raw_processing.c:1260-1284). A reduced proxy input
                 * reported as scale 1 made SH double-reduce and mis-blur.
                 * Report the EFFECTIVE scale instead: half input behaves
                 * like the proven x2 lane, quarter like the proven x4 lane.
                 * This also re-enables true mid-dim processing for the
                 * Quarter level (the item-3 upscale-first workaround is
                 * gone). */
                mlv_sync_processing_black_white_levels(video);
                const double processing_start = mlv_stage_timing_now();
                const int previous_preview_scale_factor =
                    processingPlaybackPreviewScaleFactor();
                processingSetPlaybackPreviewScaleFactor(normalizedScale << usedHalvings);
                applyProcessingObject( video->processing,
                                       mid_w, mid_h,
                                       unprocessed_frame,
                                       processed_mid,
                                       threads, 1, frameIndex );
                processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
                if (usedHalvings >= 2)
                {
                    /* Two verified 2x passes for the quarter level; the
                     * intermediate half image reuses the bayer-side TLS u16
                     * slot (dead after the decode). */
                    const int half_w = full_w / 2;
                    const int half_h = full_h / 2;
                    uint16_t * tmp_half = mlv_ensure_thread_u16_buffer(
                        (uint64_t)half_w * (uint64_t)half_h * 3u);
                    if (tmp_half)
                    {
                        mlv_rgb_u16_upscale_to_size(processed_mid, mid_w, mid_h,
                                                    tmp_half, half_w, half_h, threads);
                        mlv_rgb_u16_upscale_to_size(tmp_half, half_w, half_h,
                                                    outputFrame, width, height, threads);
                    }
                    else
                    {
                        mlv_rgb_u16_upscale_to_size(processed_mid, mid_w, mid_h,
                                                    outputFrame, width, height, threads);
                    }
                }
                else
                {
                    mlv_rgb_u16_upscale_to_size(processed_mid, mid_w, mid_h,
                                                outputFrame, width, height, threads);
                }
                g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
                mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);
                goto processed16_store;
            }
            /* TLS allocation failed: fall back to the full-res pipeline by
             * re-rendering the proxy with its internal upscale. */
        }
        int proxyRenderOk = 0;
        if (processingPlaybackPreviewModeEnabled()
         && mlv_halfres_x1_preview_enabled())
        {
            for (int h = proxyHalvings; h >= 1 && !proxyRenderOk; --h)
            {
                proxyRenderOk = mlv_render_scaled_rgb16_x1_half_preview_core(
                    video, frameIndex, NULL, unprocessed_frame, threads,
                    /*upscale_to_full*/1, h, NULL, NULL);
            }
        }
        if (proxyRenderOk)
        {
            /* Preview proxy already populated unprocessed_frame (path 6/10). */
        }
        else
        {
            const double debayer_start = mlv_stage_timing_now();
            getMlvRawFrameDebayered(video, frameIndex, unprocessed_frame);
            g_mlv_last_debayered_frame_ms = (mlv_stage_timing_now() - debayer_start) * 1000.0;
            mlv_stage_timing_note_elapsed("debayered_frame", frameIndex, g_mlv_last_debayered_frame_ms);
        }
    }

    mlv_sync_processing_black_white_levels(video);

    /* Do processing.......... */
    const double processing_start = mlv_stage_timing_now();
    const int previous_preview_scale_factor =
        processingPlaybackPreviewScaleFactor();
    processingSetPlaybackPreviewScaleFactor(normalizedScale);
    applyProcessingObject( video->processing,
                           width, height,
                           unprocessed_frame,
                           outputFrame,
                           threads, 1, frameIndex );
    processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
    g_mlv_last_processing_ms = (mlv_stage_timing_now() - processing_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processing", frameIndex, g_mlv_last_processing_ms);

processed16_store:
    if (store_processed16_cache)
    {
        /* Phase E7 recompute, narrowed by round-4 item R4-1: a render can
         * mutate llrawproc status words / black-white levels (first frame of
         * a clip), which made a pre-render signature unfindable on the next
         * lookup. Only that small mutable subset can change inside a render,
         * so pay the full ~1.1MB recompute only when the fingerprint actually
         * drifted (once per clip/state change) instead of on every frame. */
        const uint64_t final_signature =
            (requested_signature != 0
             && mlv_render_mutable_state_fingerprint(video) == entry_state_fingerprint)
            ? requested_signature
            : mlv_processed_frame_signature_with_scale(video,
                                                       frameIndex,
                                                       normalizedScale);
        uint16_t * processed_cache = mlv_ensure_u16_buffer(&video->rgb_processed_current_frame,
                                                           &video->rgb_processed_current_frame_words,
                                                           rgb_frame_size);
        if (processed_cache)
        {
            if (outputFrame != processed_cache)
            {
                memcpy(processed_cache, outputFrame, (size_t)rgb_frame_size * sizeof(uint16_t));
            }
            video->current_processed_frame_active = 1;
            video->current_processed_frame = frameIndex;
            video->current_processed_frame_threads = threads;
            video->current_processed_frame_signature = final_signature;
        }
        else
        {
            video->current_processed_frame_active = 0;
            video->current_processed_frame_signature = 0;
        }

        mlv_store_processed_frame_16bit_cache_with_scale(video,
                                                         frameIndex,
                                                         threads,
                                                         final_signature,
                                                         outputFrame,
                                                         rgb_frame_size,
                                                         normalizedScale);
    }
    else
    {
        video->current_processed_frame_active = 0;
        video->current_processed_frame_signature = 0;
    }

    g_mlv_last_processed16_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
    mlv_stage_timing_note("processed16_total", frameIndex, total_start);
}

/* Phase 4A: scale-1 entrypoint preserves the original public API. */
void getMlvProcessedFrame16(mlvObject_t * video, uint64_t frameIndex, uint16_t * outputFrame, int threads)
{
    getMlvProcessedFrame16_with_scale(video, frameIndex, outputFrame, threads, 1, 1, 0);
}

/* Phase 4A: scale-aware variant. The pipeline still renders at full
 * resolution; scaleFactor only affects cache-key isolation. */
void getMlvProcessedFrame16Scaled(mlvObject_t * video,
                                  uint64_t frameIndex,
                                  uint16_t * outputFrame,
                                  int threads,
                                  int scaleFactor)
{
    getMlvProcessedFrame16_with_scale(video, frameIndex, outputFrame, threads, scaleFactor, 1, 0);
}

/* Get a processed frame in 8 bit */
static void getMlvProcessedFrame8_with_scale(mlvObject_t * video,
                                             uint64_t frameIndex,
                                             uint8_t * outputFrame,
                                             int threads,
                                             int scaleFactor,
                                             int playbackPreview)
{
    const double total_start = mlv_stage_timing_now();
    const int previous_preview_mode = processingPlaybackPreviewModeEnabled();
    const int previous_aggressive_preview_mode =
        processingPlaybackAggressivePreviewModeEnabled();
    const int previous_preview_scale_factor =
        processingPlaybackPreviewScaleFactor();
    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_debayered_frame_ms = 0.0;
    g_mlv_last_processing_ms = 0.0;
    g_mlv_last_processed16_total_ms = 0.0;
    g_mlv_last_processed16_for_8bit_ms = 0.0;
    g_mlv_last_processed16_to_8bit_ms = 0.0;
    g_mlv_last_processed16_cache_store_ms = 0.0;
    g_mlv_last_processed8_total_ms = 0.0;
    g_mlv_last_processed8_cache_store_ms = 0.0;
    g_mlv_last_processed8_direct_path_active = 0;
    g_mlv_last_processed8_cache_hit = 0;
    g_mlv_last_processed8_cache_hit_scale_factor = 0;
    g_mlv_last_processed8_prefetch_hit = 0;
    /* Keep the playback-preview policy visible on the main render thread so
     * the direct8 gate sees the same state as the prefetch worker.
     * Round-4 item 0b: ONLY for playback callers (getMlvProcessedFrame8Scaled).
     * The unscaled getMlvProcessedFrame8 entry serves exports and single-frame
     * grabs; forcing preview semantics there routed direct8-eligible receipts
     * through the levels-skipping intrin kernel and non-eligible ones through
     * the x1 half-res playback proxy - exports must never be proxied. */
    processingSetPlaybackPreviewMode(playbackPreview ? 1 : 0);
    processingSetPlaybackAggressivePreviewMode(
        playbackPreview ? mlvPlaybackAggressivePreviewMode() : 0);

    /* Phase 4B: resolve effective scale (rejects scales that don't divide
     * the sensor evenly). */
    const int normalizedScale = mlv_effective_playback_scale_factor(video, scaleFactor);
    if (video)
    {
        video->playback_scale_factor_active = normalizedScale;
    }
    processingSetPlaybackPreviewScaleFactor(normalizedScale);

    /* Phase 4B: size the output by *effective* scale, not full sensor.
     * The downstream direct8 path reads playback_scale_factor_active to
     * decide whether to invoke the fused downsample. */
    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w  = (normalizedScale > 1) ? (full_w / normalizedScale) : full_w;
    const int out_h  = (normalizedScale > 1) ? (full_h / normalizedScale) : full_h;
    uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3u;
    uint16_t * processed_frame = NULL;
    const int direct8PathActive = mlv_can_use_direct_processed_frame8_path(video)
        && mlv_preview_direct8_input_is_cheap(video, normalizedScale);
    /* Processed8 prefetch can run even when the foreground direct8 path
     * stays conservative; the worker only warms the cache and does not
     * change the pixels we present. */
    const int processed8PrefetchActive = mlv_processed8_prefetch_enabled(video);
    /* 2026-06-11 (loop iteration 4): aggressive x4/x8 consult the cache again.
     * The skip existed because nothing useful ever landed in the cache for
     * direct8-incompatible aggressive lanes (the foreground store stays
     * skipped below - see mlv_store_processed_frame_8bit_cache_with_scale).
     * The prefetch worker's indirect render now fills honest frames ahead at
     * scale>=4, so the lookup is worth its mutex check there. x2 keeps the
     * skip: the worker cannot outpace the foreground at x2 (iteration 2). */
    const int skip_processed8_main_cache =
        mlvPlaybackAggressivePreviewMode()
     && ((normalizedScale == 1)
      || (!direct8PathActive && normalizedScale == 2));
    uint64_t requested_state_signature = 0;

    if (direct8PathActive || processed8PrefetchActive)
    {
        mlv_sync_processing_black_white_levels(video);
        requested_state_signature = mlv_processed_frame_state_signature_with_scale(video,
                                                                                   normalizedScale);
        if (processed8PrefetchActive)
        {
            mlv_processed8_prefetch_note_request(video,
                                                frameIndex,
                                                threads,
                                                requested_state_signature,
                                                normalizedScale);
        }
    }

    uint64_t requested_signature = (direct8PathActive || processed8PrefetchActive)
        ? mlv_processed_frame_signature_from_state(requested_state_signature, frameIndex)
        : (skip_processed8_main_cache
            ? 0
            : mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale));

    /* Round-4 item R4-1: snapshot for the direct8 store below -- see
     * mlv_render_mutable_state_fingerprint. */
    const uint64_t entry_state_fingerprint =
        direct8PathActive ? mlv_render_mutable_state_fingerprint(video) : 0;

    int prefetched_hit = 0;
    int cached_phase4b_path = 0;
    int cached_phase4b_y_crop_rows = 0;
    if (!skip_processed8_main_cache
        && mlv_processed_frame_8bit_cache_try_copy(video,
                                                   frameIndex,
                                                   threads,
                                                   requested_signature,
                                                   processed8PrefetchActive,
                                                   outputFrame,
                                                   rgb_frame_size,
                                                   normalizedScale,
                                                   &prefetched_hit,
                                                   &cached_phase4b_path,
                                                   &cached_phase4b_y_crop_rows))
    {
        g_mlv_last_processed8_cache_hit = 1;
        g_mlv_last_processed8_cache_hit_scale_factor = normalizedScale;
        g_mlv_last_processed8_prefetch_hit = prefetched_hit;
        g_mlv_phase4bv2_path_taken = cached_phase4b_path;
        g_mlv_phase4bv3_y_crop_rows = cached_phase4b_y_crop_rows;
        if (normalizedScale > 1 && cached_phase4b_path == 0)
        {
            mlv_phase4bv2_set_fallback_reason("processed8 cache hit without reduced-path metadata");
        }
        else
        {
            mlv_phase4bv2_set_fallback_reason("none");
        }
        if (mlv_processed8_prefetch_debug_enabled() && processed8PrefetchActive)
        {
            fprintf(stderr,
                    "[P8FG] frame=%llu HIT prefetched=%d sig=%016llx scale=%d threads=%d\n",
                    (unsigned long long)frameIndex,
                    prefetched_hit,
                    (unsigned long long)requested_signature,
                    normalizedScale,
                    threads);
        }
        g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
        mlv_stage_timing_note("processed8_total", frameIndex, total_start);
        goto cleanup;
    }

    if (mlv_processed8_prefetch_debug_enabled() && processed8PrefetchActive)
    {
        fprintf(stderr,
                "[P8FG] frame=%llu MISS sig=%016llx scale=%d threads=%d direct8=%d\n",
                (unsigned long long)frameIndex,
                (unsigned long long)requested_signature,
                normalizedScale,
                threads,
                direct8PathActive);
    }

    if (direct8PathActive)
    {
        if (!mlv_render_processed_frame8_direct_with_processing(video,
                                                                video->processing,
                                                                1,
                                                                frameIndex,
                                                                outputFrame,
                                                                threads,
                                                                normalizedScale,
                                                                1))
        {
            pthread_mutex_lock(&video->processed8_prefetch_mutex);
            video->current_processed_frame_8bit_active = 0;
            mlv_reset_processed_frame_8bit_cache_locked(video);
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
            mlv_stage_timing_note("processed8_total", frameIndex, total_start);
            goto cleanup;
        }

        /* Phase E7: render mutates llrawproc state (e.g. fpm_status, bpm_status
         * transition from 0 to 1/2 on first frame) which is part of the cache
         * state hash. Storing with the PRE-render `requested_signature` makes
         * the slot unfindable on the next call -- the second call computes a
         * fresh signature against the post-render state and misses the slot.
         * Round-4 item R4-1 narrows the recompute: only the small mutable
         * fingerprint subset can drift inside a render, so pay the full hash
         * only when it actually did (once per clip/state change). */
        const uint64_t stored_signature =
            (requested_signature != 0
             && mlv_render_mutable_state_fingerprint(video) == entry_state_fingerprint)
            ? requested_signature
            : mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale);
        const int stored_phase4b_path = mlv_phase4bv2_last_path_taken();
        const int stored_phase4b_y_crop_rows = mlv_phase4bv3_last_y_crop_rows();

        mlv_store_processed_frame_8bit_cache_with_scale(video,
                                                        frameIndex,
                                                        threads,
                                                        stored_signature,
                                                        outputFrame,
                                                        rgb_frame_size,
                                                        1,
                                                        0,
                                                        normalizedScale,
                                                        stored_phase4b_path,
                                                        stored_phase4b_y_crop_rows);

        /* S5_processed8 capture (direct8 fast path) */
        if (mlv_pipeline_capture_should_capture_frame(frameIndex))
        {
            mlv_pipeline_capture_meta_t meta;
            memset(&meta, 0, sizeof meta);
            meta.stage = MLV_PIPELINE_STAGE_S5_PROCESSED8;
            meta.format = MLV_PIPELINE_FORMAT_UINT8_RGB;
            meta.format_label = "uint8_rgb_direct8";
            meta.width = out_w;
            meta.height = out_h;
            meta.bytes_per_line = out_w * 3;
            meta.bytes_per_pixel = 3;
            meta.channels = 3;
            meta.bit_depth = 8;
            meta.scaler = (normalizedScale > 1) ? "playback_downsample" : "none";
            meta.path_label = "direct8";
            meta.settings_hash = (uint64_t)requested_signature;
            mlv_pipeline_capture(frameIndex, outputFrame, &meta);
        }

        g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
        goto cleanup;
    }

    if (video->current_processed_frame_active
        && video->current_processed_frame == frameIndex
        && video->current_processed_frame_threads == threads
        && video->current_processed_frame_signature == requested_signature
        && video->rgb_processed_current_frame)
    {
        processed_frame = video->rgb_processed_current_frame;
    }
    else
    {
        processed_frame = mlv_ensure_u16_buffer(&video->rgb_processed_current_frame,
                                                &video->rgb_processed_current_frame_words,
                                                rgb_frame_size);
        if (!processed_frame)
        {
            memset(outputFrame, 0, (size_t)rgb_frame_size);
            video->current_processed_frame_active = 0;
            pthread_mutex_lock(&video->processed8_prefetch_mutex);
            mlv_reset_processed_frame_8bit_cache_locked(video);
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
            mlv_stage_timing_note("processed8_total", frameIndex, total_start);
            goto cleanup;
        }
    }

    const double processed16_start = mlv_stage_timing_now();
    getMlvProcessedFrame16_with_scale(video,
                                      frameIndex,
                                      processed_frame,
                                      threads,
                                      normalizedScale,
                                      skip_processed8_main_cache ? 0 : 1,
                                      requested_signature);
    g_mlv_last_processed16_for_8bit_ms = (mlv_stage_timing_now() - processed16_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed16_for_8bit", frameIndex, g_mlv_last_processed16_for_8bit_ms);

    /* When the outer processed8 cache is already disabled for aggressive x4,
     * the 16-bit render is only an intermediate packdown buffer, so keep it
     * out of the exact processed16 cache as well. */
    if (!skip_processed8_main_cache
        && video->current_processed_frame_active
        && video->current_processed_frame == frameIndex
        && video->current_processed_frame_threads == threads
        && video->rgb_processed_current_frame)
    {
        processed_frame = video->rgb_processed_current_frame;
    }

    /* Copy (and 8-bitize) */
    const double convert_start = mlv_stage_timing_now();
    const size_t row_words = (size_t)out_w * 3u;
    #pragma omp parallel for schedule(static)
    for (int y = 0; y < out_h; ++y)
    {
        const uint16_t * __restrict src_row =
            processed_frame + ((size_t)y * row_words);
        uint8_t * __restrict dst_row =
            outputFrame + ((size_t)y * row_words);
        for (size_t x = 0; x < row_words; ++x)
        {
            dst_row[x] = (uint8_t)(src_row[x] >> 8);
        }
    }
    g_mlv_last_processed16_to_8bit_ms = (mlv_stage_timing_now() - convert_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed16_to_8bit", frameIndex, g_mlv_last_processed16_to_8bit_ms);

    /* S5_processed8 capture (indirect path: processed16 -> 8) */
    if (mlv_pipeline_capture_should_capture_frame(frameIndex))
    {
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = MLV_PIPELINE_STAGE_S5_PROCESSED8;
        meta.format = MLV_PIPELINE_FORMAT_UINT8_RGB;
        meta.format_label = "uint8_rgb_processed16_packdown";
        meta.width = out_w;
        meta.height = out_h;
        meta.bytes_per_line = out_w * 3;
        meta.bytes_per_pixel = 3;
        meta.channels = 3;
        meta.bit_depth = 8;
        meta.scaler = (normalizedScale > 1) ? "playback_downsample" : "none";
        meta.path_label = "processed16_to_8";
        meta.settings_hash =
            (uint64_t)mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale);
        mlv_pipeline_capture(frameIndex, outputFrame, &meta);
    }

    if (!skip_processed8_main_cache)
    {
        mlv_store_processed_frame_8bit_cache_with_scale(
            video,
            frameIndex,
            threads,
            video->current_processed_frame_active
                ? video->current_processed_frame_signature
                : mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale),
            outputFrame,
            rgb_frame_size,
            1,
            0,
            normalizedScale,
            mlv_phase4bv2_last_path_taken(),
            mlv_phase4bv3_last_y_crop_rows());
    }

    g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
cleanup:
    processingSetPlaybackPreviewScaleFactor(previous_preview_scale_factor);
    processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
    processingSetPlaybackPreviewMode(previous_preview_mode);
}

/* Phase 4A: scale-1 entrypoint preserves the original public API. */
/* Non-playback entry (exports, single-frame grabs): full quality, no preview
 * proxies, non-preview direct8 contract. */
void getMlvProcessedFrame8(mlvObject_t * video, uint64_t frameIndex, uint8_t * outputFrame, int threads)
{
    getMlvProcessedFrame8_with_scale(video, frameIndex, outputFrame, threads, 1,
                                     /*playbackPreview*/0);
}

/* Phase 4A: scale-aware variant. The pipeline still renders at full
 * resolution; scaleFactor only affects cache-key isolation. Playback entry:
 * preview semantics (proxies, preview direct8 gate) apply. */
void getMlvProcessedFrame8Scaled(mlvObject_t * video,
                                 uint64_t frameIndex,
                                 uint8_t * outputFrame,
                                 int threads,
                                 int scaleFactor)
{
    getMlvProcessedFrame8_with_scale(video, frameIndex, outputFrame, threads, scaleFactor,
                                     /*playbackPreview*/1);
}

/* PRECONDITION (playback-preview envelope): unlike getMlvProcessedFrame8Scaled
 * / getMlvProcessedFrame8_with_scale -- which save, set and restore the FULL
 * preview envelope themselves (preview mode, aggressive-preview mode AND the
 * processing-global preview scale factor) -- this helper and its sibling
 * getMlvProcessedFrame8ScaledFromReconnedRaw16 deliberately establish ONLY the
 * aggressive-preview sub-flag and set video->playback_scale_factor_active
 * directly. They INHERIT processingSetPlaybackPreviewMode(1) and
 * processingSetPlaybackPreviewScaleFactor(...) from the caller's envelope.
 *
 * The only production caller is RenderFrameThread, which wraps the whole
 * render body in PlaybackPreviewModeGuard (platform/qt/RenderFrameThread.cpp)
 * before invoking these helpers; the guard is what sets the preview mode and
 * preview scale factor for the call's duration. These helpers do not
 * re-establish that envelope so the render hot path is not double-guarded.
 *
 * Subtlety: the guard sets the global preview scale factor to the RAW requested
 * scale, whereas getMlvProcessedFrame8_with_scale sets it to the EFFECTIVE
 * scale (mlv_effective_playback_scale_factor). They agree for the evenly
 * dividing scales the UI offers, but on a down-normalized (misaligned) scale
 * the inherited global stays at the raw value while
 * video->playback_scale_factor_active holds the effective one, so a global
 * reader (e.g. the quarter-res shadows/highlights RBF gate in raw_processing.c)
 * sees the requested, not the effective, scale on this inherited path. Aligning
 * them by self-establishing the envelope here (like with_scale) is a behavior
 * change deferred to its own review.
 *
 * Consequence: calling this helper OUTSIDE an active playback-preview envelope
 * (preview mode disabled, or a stale/foreign preview scale factor) gives
 * incorrect direct8 gating and playback-proxy behavior. This is NOT a
 * general-purpose 8-bit entry point -- use getMlvProcessedFrame8 (exports /
 * single-frame grabs) or getMlvProcessedFrame8Scaled (which establishes the
 * envelope itself) when calling from outside the render thread. */
int getMlvProcessedFrame8ScaledFromRaw16(mlvObject_t * video,
                                         uint64_t frameIndex,
                                         const uint16_t * decodedRawFrame,
                                         uint8_t * outputFrame,
                                         int threads,
                                         int scaleFactor)
{
    const double total_start = mlv_stage_timing_now();
    const int previous_preview_mode = processingPlaybackPreviewModeEnabled();
    const int previous_aggressive_preview_mode =
        processingPlaybackAggressivePreviewModeEnabled();
    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_raw_uint16_ms = 0.0;
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_debayered_frame_ms = 0.0;
    g_mlv_last_processing_ms = 0.0;
    g_mlv_last_processed16_total_ms = 0.0;
    g_mlv_last_processed16_for_8bit_ms = 0.0;
    g_mlv_last_processed16_to_8bit_ms = 0.0;
    g_mlv_last_processed16_cache_store_ms = 0.0;
    g_mlv_last_processed8_total_ms = 0.0;
    g_mlv_last_processed8_cache_store_ms = 0.0;
    g_mlv_last_processed8_direct_path_active = 0;
    g_mlv_last_processed8_cache_hit = 0;
    g_mlv_last_processed8_cache_hit_scale_factor = 0;
    g_mlv_last_processed8_prefetch_hit = 0;

    if (!video || !decodedRawFrame || !outputFrame) return 0;
    processingSetPlaybackAggressivePreviewMode(mlvPlaybackAggressivePreviewMode());

    const int normalizedScale = mlv_effective_playback_scale_factor(video, scaleFactor);
    video->playback_scale_factor_active = normalizedScale;

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = (normalizedScale > 1) ? (full_w / normalizedScale) : full_w;
    const int out_h = (normalizedScale > 1) ? (full_h / normalizedScale) : full_h;
    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3u;
    if (!mlv_can_use_direct_processed_frame8_path(video) || normalizedScale <= 1)
    {
        processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
        processingSetPlaybackPreviewMode(previous_preview_mode);
        return 0;
    }

    mlv_sync_processing_black_white_levels(video);
    const uint64_t requested_state_signature =
        mlv_processed_frame_state_signature_with_scale(video, normalizedScale);
    const uint64_t requested_signature =
        mlv_processed_frame_signature_from_state(requested_state_signature, frameIndex);
    const int skip_processed8_main_cache =
        mlvPlaybackAggressivePreviewMode() && normalizedScale >= 2;

    if (!mlv_render_processed_frame8_direct_with_processing_from_raw(video,
                                                                      video->processing,
                                                                      0,
                                                                      frameIndex,
                                                                      decodedRawFrame,
                                                                      outputFrame,
                                                                      threads,
                                                                      normalizedScale,
                                                                      1))
    {
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        video->current_processed_frame_8bit_active = 0;
        mlv_reset_processed_frame_8bit_cache_locked(video);
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
        g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
        processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
        processingSetPlaybackPreviewMode(previous_preview_mode);
        return 0;
    }

    const uint64_t stored_signature =
        mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale);
    const int stored_phase4b_path = mlv_phase4bv2_last_path_taken();
    const int stored_phase4b_y_crop_rows = mlv_phase4bv3_last_y_crop_rows();
    if (!skip_processed8_main_cache)
    {
        mlv_store_processed_frame_8bit_cache_with_scale(video,
                                                        frameIndex,
                                                        threads,
                                                        stored_signature ? stored_signature : requested_signature,
                                                        outputFrame,
                                                        rgb_frame_size,
                                                        1,
                                                        0,
                                                        normalizedScale,
                                                        stored_phase4b_path,
                                                        stored_phase4b_y_crop_rows);
    }

    g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
    processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
    processingSetPlaybackPreviewMode(previous_preview_mode);
    return 1;
}

/* Same playback-preview envelope precondition as
 * getMlvProcessedFrame8ScaledFromRaw16 (see the contract note above it): must
 * run inside the render thread's PlaybackPreviewModeGuard; manages only the
 * aggressive-preview sub-flag and inherits preview mode / preview scale factor
 * from that envelope. Not a general-purpose entry point. */
int getMlvProcessedFrame8ScaledFromReconnedRaw16(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 const uint16_t * reconnedRawFrame,
                                                 uint8_t * outputFrame,
                                                 int threads,
                                                 int scaleFactor,
                                                 int allowScale1StateDebayer)
{
    const double total_start = mlv_stage_timing_now();
    const int previous_preview_mode = processingPlaybackPreviewModeEnabled();
    const int previous_aggressive_preview_mode =
        processingPlaybackAggressivePreviewModeEnabled();
    mlv_reset_last_raw_stage_telemetry();
    g_mlv_last_raw_uint16_ms = 0.0;
    g_mlv_last_llrawproc_ms = 0.0;
    g_mlv_last_debayered_frame_ms = 0.0;
    g_mlv_last_processing_ms = 0.0;
    g_mlv_last_processed16_total_ms = 0.0;
    g_mlv_last_processed16_for_8bit_ms = 0.0;
    g_mlv_last_processed16_to_8bit_ms = 0.0;
    g_mlv_last_processed16_cache_store_ms = 0.0;
    g_mlv_last_processed8_total_ms = 0.0;
    g_mlv_last_processed8_cache_store_ms = 0.0;
    g_mlv_last_processed8_direct_path_active = 0;
    g_mlv_last_processed8_cache_hit = 0;
    g_mlv_last_processed8_cache_hit_scale_factor = 0;
    g_mlv_last_processed8_prefetch_hit = 0;

    if (!video || !reconnedRawFrame || !outputFrame) return 0;
    processingSetPlaybackAggressivePreviewMode(mlvPlaybackAggressivePreviewMode());

    const int normalizedScale = mlv_effective_playback_scale_factor(video, scaleFactor);
    video->playback_scale_factor_active = normalizedScale;

    const int full_w = (int)getMlvWidth(video);
    const int full_h = (int)getMlvHeight(video);
    const int out_w = (normalizedScale > 1) ? (full_w / normalizedScale) : full_w;
    const int out_h = (normalizedScale > 1) ? (full_h / normalizedScale) : full_h;
    const uint64_t rgb_frame_size = (uint64_t)out_w * (uint64_t)out_h * 3u;
    if (!mlv_can_use_direct_processed_frame8_path(video)
     || (normalizedScale <= 1 && !allowScale1StateDebayer))
    {
        processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
        processingSetPlaybackPreviewMode(previous_preview_mode);
        return 0;
    }

    const uint64_t requested_state_signature =
        mlv_processed_frame_state_signature_with_scale(video, normalizedScale);
    const uint64_t requested_signature =
        mlv_processed_frame_signature_from_state(requested_state_signature, frameIndex);
    const int skip_processed8_main_cache =
        mlvPlaybackAggressivePreviewMode() && normalizedScale >= 2;

    if (!mlv_render_processed_frame8_direct_with_processing_from_reconned_raw(video,
                                                                              video->processing,
                                                                              1,
                                                                              frameIndex,
                                                                              reconnedRawFrame,
                                                                              outputFrame,
                                                                              threads,
                                                                              normalizedScale,
                                                                              allowScale1StateDebayer,
                                                                              1))
    {
        pthread_mutex_lock(&video->processed8_prefetch_mutex);
        video->current_processed_frame_8bit_active = 0;
        mlv_reset_processed_frame_8bit_cache_locked(video);
        pthread_mutex_unlock(&video->processed8_prefetch_mutex);
        g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
        mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
        processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
        processingSetPlaybackPreviewMode(previous_preview_mode);
        return 0;
    }

    const uint64_t stored_signature =
        mlv_processed_frame_signature_with_scale(video, frameIndex, normalizedScale);
    const int stored_phase4b_path = mlv_phase4bv2_last_path_taken();
    const int stored_phase4b_y_crop_rows = mlv_phase4bv3_last_y_crop_rows();
    if (!skip_processed8_main_cache)
    {
        mlv_store_processed_frame_8bit_cache_with_scale(video,
                                                        frameIndex,
                                                        threads,
                                                        stored_signature ? stored_signature : requested_signature,
                                                        outputFrame,
                                                        rgb_frame_size,
                                                        1,
                                                        0,
                                                        normalizedScale,
                                                        stored_phase4b_path,
                                                        stored_phase4b_y_crop_rows);
    }

    g_mlv_last_processed8_total_ms = (mlv_stage_timing_now() - total_start) * 1000.0;
    mlv_stage_timing_note_elapsed("processed8_total", frameIndex, g_mlv_last_processed8_total_ms);
    processingSetPlaybackAggressivePreviewMode(previous_aggressive_preview_mode);
    processingSetPlaybackPreviewMode(previous_preview_mode);
    return 1;
}

/* Phase 4B: helper for callers that need to size their output buffer for
 * the scaled pipeline. Returns (W/scale, H/scale) when the requested
 * scale is honoured, else returns the full sensor dimensions.
 *
 * The "effective" scale is computed with the same alignment rules as the
 * rendering path. Explicit scale=2 is honored for Dual ISO by using the
 * safe full-recon-then-downsample path instead of the scale=4 pre-recon
 * shortcut. */
void mlvFrameOutputDimensions(mlvObject_t * video,
                              int scaleFactor,
                              int * outWidth,
                              int * outHeight)
{
    const int width  = video ? (int)getMlvWidth(video) : 0;
    const int height = video ? (int)getMlvHeight(video) : 0;
    const int s = mlv_effective_playback_scale_factor(video, scaleFactor);
    if (outWidth)
    {
        *outWidth = (s > 1) ? (width / s) : width;
    }
    if (outHeight)
    {
        *outHeight = (s > 1) ? (height / s) : height;
    }
}

double getMlvLastRawUint16Milliseconds(void)
{
    return g_mlv_last_raw_uint16_ms;
}

double getMlvLastRawUint16DiskReadMilliseconds(void)
{
    return g_mlv_last_raw_uint16_disk_read_ms;
}

double getMlvLastRawUint16DecompressMilliseconds(void)
{
    return g_mlv_last_raw_uint16_decompress_ms;
}

double getMlvLastRawUint16DecompressPrepareMilliseconds(void)
{
    return g_mlv_last_raw_uint16_decompress_prepare_ms;
}

double getMlvLastRawUint16DecompressExecuteMilliseconds(void)
{
    return g_mlv_last_raw_uint16_decompress_execute_ms;
}

int getMlvLastRawUint16Lj92Pred6SplitActive(void)
{
    return g_mlv_last_raw_uint16_lj92_pred6_split_active;
}

int getMlvLastRawUint16Lj92Pred6SplitRequested(void)
{
    return g_mlv_last_raw_uint16_lj92_pred6_split_requested;
}

int getMlvLastRawUint16Lj92GenericSplitActive(void)
{
    return g_mlv_last_raw_uint16_lj92_generic_split_active;
}

int getMlvLastRawUint16Lj92GenericSplitRequested(void)
{
    return g_mlv_last_raw_uint16_lj92_generic_split_requested;
}

int getMlvLastRawUint16Lj92Pred1FastPathActive(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_active;
}

int getMlvLastRawUint16Lj92Pred1FastPathMeasurementRequested(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_requested;
}

int getMlvLastRawUint16Lj92Pred1FastPathMeasurementActive(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_measurement_active;
}

int getMlvLastRawUint16Lj92Pred1FastPathEligible(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_eligible;
}

int getMlvLastRawUint16Lj92ScanComponentCount(void)
{
    return g_mlv_last_raw_uint16_lj92_scan_component_count;
}

int getMlvLastRawUint16Lj92WriteLength(void)
{
    return g_mlv_last_raw_uint16_lj92_write_length;
}

int getMlvLastRawUint16Lj92ExpectedWriteLength(void)
{
    return g_mlv_last_raw_uint16_lj92_expected_write_length;
}

int getMlvLastRawUint16Lj92SkipLength(void)
{
    return g_mlv_last_raw_uint16_lj92_skip_length;
}

int getMlvLastRawUint16Lj92LinearizeActive(void)
{
    return g_mlv_last_raw_uint16_lj92_linearize_active;
}

int getMlvLastRawUint16Lj92ComponentCount(void)
{
    return g_mlv_last_raw_uint16_lj92_component_count;
}

int getMlvLastRawUint16Lj92Predictor(void)
{
    return g_mlv_last_raw_uint16_lj92_predictor;
}

double getMlvLastRawUint16Lj92Pred6TotalMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred6_total_ms;
}

double getMlvLastRawUint16Lj92Pred6BitstreamMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred6_bitstream_ms;
}

double getMlvLastRawUint16Lj92Pred6PredictorMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred6_predictor_ms;
}

double getMlvLastRawUint16Lj92GenericTotalMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_generic_total_ms;
}

double getMlvLastRawUint16Lj92GenericBitstreamMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_generic_bitstream_ms;
}

double getMlvLastRawUint16Lj92GenericPredictorMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_generic_predictor_ms;
}

double getMlvLastRawUint16Lj92Pred1FastPathTotalMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_total_ms;
}

double getMlvLastRawUint16Lj92Pred1FastPathBitstreamMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_bitstream_ms;
}

double getMlvLastRawUint16Lj92Pred1FastPathPredictorMilliseconds(void)
{
    return g_mlv_last_raw_uint16_lj92_pred1_fast_path_predictor_ms;
}

double getMlvLastRawUint16UnpackMilliseconds(void)
{
    return g_mlv_last_raw_uint16_unpack_ms;
}

double getMlvLastRawUint16CopyMilliseconds(void)
{
    return g_mlv_last_raw_uint16_copy_ms;
}

int getMlvLastRawUint16PrefetchHit(void)
{
    return g_mlv_last_raw_uint16_prefetch_hit;
}

int mlvRawUint16PrefetchAllowedForTesting(const mlvObject_t * video)
{
    return mlv_raw_uint16_prefetch_allowed_for_request(video);
}

uint64_t getMlvRawUint16PrefetchDecodeFailures(mlvObject_t * video)
{
    if (!video) return 0;
    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    uint64_t value = video->raw_uint16_prefetch_decode_failures;
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    return value;
}

double getMlvLastLlrawprocMilliseconds(void)
{
    return g_mlv_last_llrawproc_ms;
}

double getMlvLastRawFloatConvertMilliseconds(void)
{
    return g_mlv_last_raw_float_convert_ms;
}

double getMlvLastDebayeredFrameMilliseconds(void)
{
    return g_mlv_last_debayered_frame_ms;
}

double getMlvLastProcessingMilliseconds(void)
{
    return g_mlv_last_processing_ms;
}

double getMlvLastProcessed16TotalMilliseconds(void)
{
    return g_mlv_last_processed16_total_ms;
}

double getMlvLastProcessed16For8BitMilliseconds(void)
{
    return g_mlv_last_processed16_for_8bit_ms;
}

double getMlvLastProcessed16To8BitMilliseconds(void)
{
    return g_mlv_last_processed16_to_8bit_ms;
}

double getMlvLastProcessed16CacheStoreMilliseconds(void)
{
    return g_mlv_last_processed16_cache_store_ms;
}

double getMlvLastProcessed8TotalMilliseconds(void)
{
    return g_mlv_last_processed8_total_ms;
}

double getMlvLastProcessed8CacheStoreMilliseconds(void)
{
    return g_mlv_last_processed8_cache_store_ms;
}

int getMlvLastProcessed8DirectPathActive(void)
{
    return g_mlv_last_processed8_direct_path_active;
}

int getMlvLastProcessed8CacheHit(void)
{
    return g_mlv_last_processed8_cache_hit;
}

int getMlvLastProcessed8CacheHitScaleFactor(void)
{
    return g_mlv_last_processed8_cache_hit_scale_factor;
}

int getMlvLastProcessed8PrefetchHit(void)
{
    return g_mlv_last_processed8_prefetch_hit;
}

int mlvWaitForProcessed8PrefetchIdleForTesting(mlvObject_t * video,
                                               uint32_t timeout_ms)
{
    if (!video)
    {
        return 1;
    }

    struct timespec deadline;
    if (clock_gettime(CLOCK_REALTIME, &deadline) != 0)
    {
        return 0;
    }
    deadline.tv_sec += (time_t)(timeout_ms / 1000u);
    deadline.tv_nsec += (long)(timeout_ms % 1000u) * 1000000L;
    if (deadline.tv_nsec >= 1000000000L)
    {
        ++deadline.tv_sec;
        deadline.tv_nsec -= 1000000000L;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    ++video->processed8_prefetch_test_idle_waiters;
    while (video->processed8_prefetch_worker_busy
           || video->processed8_prefetch_request_pending)
    {
        const int wait_rc = pthread_cond_timedwait(&video->processed8_prefetch_cond,
                                                   &video->processed8_prefetch_mutex,
                                                   &deadline);
        if (wait_rc == ETIMEDOUT)
        {
            --video->processed8_prefetch_test_idle_waiters;
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            return 0;
        }
    }
    --video->processed8_prefetch_test_idle_waiters;
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    return 1;
}

void mlvSetProcessed8PrefetchPauseBeforeStoreForTesting(mlvObject_t * video,
                                                        int enabled)
{
    if (!video)
    {
        return;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    video->processed8_prefetch_test_pause_before_store = enabled ? 1 : 0;
    pthread_cond_broadcast(&video->processed8_prefetch_cond);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
}

int mlvWaitForProcessed8PrefetchPausedBeforeStoreForTesting(mlvObject_t * video,
                                                            uint32_t timeout_ms)
{
    if (!video)
    {
        return 0;
    }

    struct timespec deadline;
    if (clock_gettime(CLOCK_REALTIME, &deadline) != 0)
    {
        return 0;
    }
    deadline.tv_sec += (time_t)(timeout_ms / 1000u);
    deadline.tv_nsec += (long)(timeout_ms % 1000u) * 1000000L;
    if (deadline.tv_nsec >= 1000000000L)
    {
        ++deadline.tv_sec;
        deadline.tv_nsec -= 1000000000L;
    }

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    while (!video->processed8_prefetch_test_paused_before_store)
    {
        const int wait_rc = pthread_cond_timedwait(&video->processed8_prefetch_cond,
                                                   &video->processed8_prefetch_mutex,
                                                   &deadline);
        if (wait_rc == ETIMEDOUT)
        {
            pthread_mutex_unlock(&video->processed8_prefetch_mutex);
            return 0;
        }
    }
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    return 1;
}

int getMlvProcessed8PrefetchEnabledForTesting(const mlvObject_t * video)
{
    return mlv_processed8_prefetch_enabled(video);
}

/* To initialise mlv object with a clip
 * Two functions in one */
mlvObject_t * initMlvObjectWithClip(char * mlvPath, int preview, int * err, char * error_message)
{
    mlvObject_t * video = initMlvObject();
    char error_message_tmp[256] = {0};
    int err_tmp =  openMlvClip(video, mlvPath, preview, error_message_tmp);
    if (err != NULL) *err = err_tmp;
    if (error_message != NULL) strcpy(error_message, error_message_tmp);
    return video;
}

/* To initialise mlv object with a clip
 * Two functions in one */
mlvObject_t * initMlvObjectWithMcrawClip(char * mlvPath, int preview, int * err, char * error_message)
{
    mlvObject_t * video = initMlvObject();
    char error_message_tmp[256] = {0};
    int err_tmp =  openMcrawClip(video, mlvPath, preview, error_message_tmp);
    if (err != NULL) *err = err_tmp;
    if (error_message != NULL) strcpy(error_message, error_message_tmp);
    return video;
}

/* Init an mlv object backed by a folder of CinemaDNG frames. Mirrors the
 * mcraw entry point above. */
mlvObject_t * initMlvObjectWithDngFolder(char * dirPath, int preview, int * err, char * error_message)
{
    mlvObject_t * video = initMlvObject();
    char error_message_tmp[256] = {0};
    int err_tmp =  openDngFolderClip(video, dirPath, preview, error_message_tmp);
    if (err != NULL) *err = err_tmp;
    if (error_message != NULL) strcpy(error_message, error_message_tmp);
    return video;
}

/* Allocates a tiny bit of memory for everything in the structure
 * so we can always be sure there is memory, and when we need to
 * resize it, simply do free followed by malloc */
mlvObject_t * initMlvObject()
{
    mlvObject_t * video = (mlvObject_t *)calloc( 1, sizeof(mlvObject_t) );

    /* Initialize index buffers with NULL,
     * will be allocated/reallocated later */
    video->video_index = NULL;
    video->audio_index = NULL;

    /* Init audio buffer pointer */
    video->audio_data = NULL;

    /* Cache things, only one element for now as it is empty */
    video->rgb_raw_frames = NULL;
    video->rgb_raw_current_frame = NULL;
    video->cached_frames = NULL;
    video->raw_debayer_temp_frame = NULL;
    video->rgb_processed_temp_frame = NULL;
    video->rgb_processed_current_frame = NULL;
    video->rgb_processed_frame_cache_16bit = NULL;
    video->rgb_processed_current_frame_8bit = NULL;
    video->current_processed_frame_signature = 0;
    mlv_reset_processed_frame_16bit_cache(video);
    mlv_reset_processed_frame_8bit_cache(video);
    /* All frames in one block of memory for least mallocing during usage */
    video->cache_memory_block = NULL;
    /* Path (so separate cache threads can have their own FILE*s) */
    video->path = NULL;

    /* Will avoid main file conflicts with audio and stuff */
    pthread_mutex_init(&video->g_mutexFind, NULL);
    pthread_mutex_init(&video->g_mutexCount, NULL);
    pthread_mutex_init(&video->g_mutexCacheLifecycle, NULL);
    pthread_mutex_init(&video->llrawproc_mutex, NULL);
    pthread_mutex_init(&video->llrawproc_worker_mutex, NULL);
    pthread_mutex_init(&video->processed8_prefetch_mutex, NULL);
    pthread_cond_init(&video->processed8_prefetch_cond, NULL);
    pthread_mutex_init(&video->raw_uint16_prefetch_mutex, NULL);
    pthread_cond_init(&video->raw_uint16_prefetch_cond, NULL);
    video->llrawproc_workers = NULL;
    video->llrawproc_worker_capacity = 0;
    video->processed8_prefetch_processing = initProcessingObject();
    video->processed8_prefetch_thread_started = 0;
    video->processed8_prefetch_stop = 0;
    video->processed8_prefetch_request_pending = 0;
    video->processed8_prefetch_worker_busy = 0;
    video->processed8_prefetch_request_frame = 0;
    video->processed8_prefetch_request_threads = 0;
    video->processed8_prefetch_last_request_frame = 0;
    video->processed8_prefetch_last_request_threads = 0;
    video->processed8_prefetch_last_state_signature = 0;
    video->processed8_prefetch_generation = 1;
    video->processed8_prefetch_snapshot_dirty = 1;
    video->processed8_prefetch_test_pause_before_store = 0;
    video->processed8_prefetch_test_paused_before_store = 0;
    video->processed8_prefetch_test_idle_waiters = 0;
    video->raw_uint16_prefetch_thread_started = 0;
    video->raw_uint16_prefetch_stop = 0;
    video->raw_uint16_prefetch_request_pending = 0;
    video->raw_uint16_prefetch_worker_busy = 0;
    video->raw_uint16_prefetch_request_frame = 0;
    video->raw_uint16_prefetch_last_request_frame = 0;
    video->raw_uint16_prefetch_request_stride = 1;
    video->raw_uint16_prefetch_generation = 1;
    video->raw_uint16_prefetch_cache = NULL;
    video->raw_uint16_prefetch_cache_words = 0;
    video->raw_uint16_prefetch_slot_words = 0;
    mlv_reset_raw_uint16_prefetch_locked(video);

    /* Set cache limit to allow ~1 second of 1080p and be safe for low ram PCs */
    setMlvRawCacheLimitMegaBytes(video, 290);
    setMlvCacheStartFrame(video, 0); /* Just in case */

    /* Seems about right */
    setMlvCpuCores(video, 4);

    /* Init low level raw processing object */
    video->llrawproc = initLLRawProcObject();

    /* Init CA correction */
    //video->ca_auto = 0;
    video->ca_red = 0.0;
    video->ca_blue = 0.0;

    /* CURV lookup table */
    video->linearise_lut = NULL;

    /* Use default camid as fallback */
    camera_id_t *camid = camidGet(0);
    memcpy(&video->camid, camid, sizeof(camera_id_t));

    /* Retun pointer */
    return video;
}

/* Free all memory and close file */
void freeMlvObject(mlvObject_t * video)
{
    if (!video) return;
    /* Close admission atomically with worker claims.  Existing lifecycle
     * owners/waiters and workers retain the object until they drain; no new
     * cache operation or detached worker can enter after this transition. */
    pthread_mutex_lock(&video->g_mutexCount);
    video->cache_closing = 1;
    video->stop_caching = 1;
    pthread_mutex_unlock(&video->g_mutexCount);
    for (;;)
    {
        pthread_mutex_lock(&video->g_mutexCount);
        const int cache_busy = video->cache_thread_count != 0
            || video->cache_lifecycle_users != 0;
        pthread_mutex_unlock(&video->g_mutexCount);
        if (!cache_busy) break;
        usleep(100);
    }

    /* is_active is read by admitted cache transactions and claimed workers.
     * Publish the inactive state only after both populations have drained, so
     * teardown never races those readers or changes an in-flight transaction. */
    pthread_mutex_lock(&video->g_mutexCount);
    isMlvActive(video) = 0;
    pthread_mutex_unlock(&video->g_mutexCount);

    pthread_mutex_lock(&video->processed8_prefetch_mutex);
    video->processed8_prefetch_stop = 1;
    pthread_cond_broadcast(&video->processed8_prefetch_cond);
    pthread_mutex_unlock(&video->processed8_prefetch_mutex);
    if (video->processed8_prefetch_thread_started)
    {
        pthread_join(video->processed8_prefetch_thread, NULL);
    }

    pthread_mutex_lock(&video->raw_uint16_prefetch_mutex);
    video->raw_uint16_prefetch_stop = 1;
    pthread_cond_broadcast(&video->raw_uint16_prefetch_cond);
    pthread_mutex_unlock(&video->raw_uint16_prefetch_mutex);
    if (video->raw_uint16_prefetch_thread_started)
    {
        pthread_join(video->raw_uint16_prefetch_thread, NULL);
    }

    /* Close all MLV file chunks */
    if(video->file) close_all_chunks(video->file, video->filenum);
    /* Free CinemaDNG folder source, if any */
    if(video->dng_sequence)
    {
        dng_sequence_free((dng_sequence_t *)video->dng_sequence);
        free(video->dng_sequence);
        video->dng_sequence = NULL;
    }
    /* Free all memory */
    if(video->video_index) free(video->video_index);
    if(video->audio_index) free(video->audio_index);
    if(video->vers_index) free(video->vers_index);

    /* Free audio buffer */
    if(video->audio_data)
    {
        free(video->audio_data);
        video->audio_data = NULL;
    }

    /* Now free these */
    if(video->cached_frames)
    {
        free(video->cached_frames);
        video->cached_frames = NULL;
    }
    if(video->rgb_raw_frames) free(video->rgb_raw_frames);
    if(video->rgb_raw_current_frame) free(video->rgb_raw_current_frame);
    if(video->raw_debayer_temp_frame) free(video->raw_debayer_temp_frame);
    if(video->rgb_processed_temp_frame) free(video->rgb_processed_temp_frame);
    if(video->rgb_processed_current_frame) free(video->rgb_processed_current_frame);
    if(video->rgb_processed_frame_cache_16bit) free(video->rgb_processed_frame_cache_16bit);
    if(video->rgb_processed_current_frame_8bit) free(video->rgb_processed_current_frame_8bit);
    if(video->raw_uint16_prefetch_cache) free(video->raw_uint16_prefetch_cache);
    if(video->cache_memory_block) free(video->cache_memory_block);
    if(video->path) free(video->path);
    if(video->processed8_prefetch_processing) freeProcessingObject(video->processed8_prefetch_processing);
    if(video->linearise_lut) free(video->linearise_lut);
    freeLLRawProcObject(video);

    /* Mutex things here... */
    for (int i = 0; i < video->filenum; ++i)
        if(video->main_file_mutex) pthread_mutex_destroy(video->main_file_mutex + i);
    if(video->main_file_mutex) free(video->main_file_mutex);
    pthread_mutex_destroy(&video->g_mutexFind);
    pthread_mutex_destroy(&video->g_mutexCount);
    pthread_mutex_destroy(&video->llrawproc_mutex);
    pthread_mutex_destroy(&video->llrawproc_worker_mutex);
    pthread_mutex_destroy(&video->processed8_prefetch_mutex);
    pthread_cond_destroy(&video->processed8_prefetch_cond);
    pthread_mutex_destroy(&video->raw_uint16_prefetch_mutex);
    pthread_cond_destroy(&video->raw_uint16_prefetch_cond);

    pthread_mutex_destroy(&video->g_mutexCacheLifecycle);

    /* Main 1 */
    free(video);
}

/* Save MLV App map file (.MAPP) */
static int save_mapp(mlvObject_t * video)
{
    int mapp_name_len = strlen(video->path);
    char * mapp_filename = alloca(mapp_name_len + 4);
    memset(mapp_filename, 0x00, mapp_name_len + 4);
    memcpy(mapp_filename, video->path, mapp_name_len);
    char * dot = strrchr(mapp_filename, '.');
    memcpy(dot, ".MAPP\0", 6);

    size_t video_index_size = video->frames * sizeof(frame_index_t);
    size_t audio_index_size = video->audios * sizeof(frame_index_t);
    size_t vers_index_size = video->vers_blocks * sizeof(frame_index_t);
    size_t mapp_buf_size = sizeof(mapp_header_t) +
                           sizeof(mlv_file_hdr_t) +
                           sizeof(mlv_rawi_hdr_t) +
                           sizeof(mlv_rawc_hdr_t) +
                           sizeof(mlv_idnt_hdr_t) +
                           sizeof(mlv_expo_hdr_t) +
                           sizeof(mlv_lens_hdr_t) +
                           sizeof(mlv_elns_hdr_t) +
                           sizeof(mlv_rtci_hdr_t) +
                           sizeof(mlv_wbal_hdr_t) +
                           sizeof(mlv_styl_hdr_t) +
                           sizeof(mlv_wavi_hdr_t) +
                           sizeof(mlv_diso_hdr_t) +
                           sizeof(mlv_dark_hdr_t) +
                           sizeof(camera_id_t) +
                           video_index_size +
                           audio_index_size +
                           vers_index_size;

    uint8_t * mapp_buf = malloc(mapp_buf_size);
    if(!mapp_buf)
    {
        return 1;
    }

    /* init mapp header */
    mapp_header_t mapp_header = { "MAPP", mapp_buf_size + video->audio_size, MAPP_VERSION, video->block_num, video->frames, video->audios, video->vers_blocks, video->audio_size, video->dark_frame_offset };
    /* copy pointer to mapp buffer */
    uint8_t * ptr = mapp_buf;
    /* fill mapp buffer */
    memcpy(ptr, (uint8_t*)&mapp_header, sizeof(mapp_header_t));
    memcpy(ptr += sizeof(mapp_header_t), (uint8_t*)&(video->MLVI), sizeof(mlv_file_hdr_t));
    memcpy(ptr += sizeof(mlv_file_hdr_t), (uint8_t*)&(video->RAWI), sizeof(mlv_rawi_hdr_t));
    memcpy(ptr += sizeof(mlv_rawi_hdr_t), (uint8_t*)&(video->RAWC), sizeof(mlv_rawc_hdr_t));
    memcpy(ptr += sizeof(mlv_rawc_hdr_t), (uint8_t*)&(video->IDNT), sizeof(mlv_idnt_hdr_t));
    memcpy(ptr += sizeof(mlv_idnt_hdr_t), (uint8_t*)&(video->EXPO), sizeof(mlv_expo_hdr_t));
    memcpy(ptr += sizeof(mlv_expo_hdr_t), (uint8_t*)&(video->LENS), sizeof(mlv_lens_hdr_t));
    memcpy(ptr += sizeof(mlv_lens_hdr_t), (uint8_t*)&(video->ELNS), sizeof(mlv_elns_hdr_t));
    memcpy(ptr += sizeof(mlv_elns_hdr_t), (uint8_t*)&(video->RTCI), sizeof(mlv_rtci_hdr_t));
    memcpy(ptr += sizeof(mlv_rtci_hdr_t), (uint8_t*)&(video->WBAL), sizeof(mlv_wbal_hdr_t));
    memcpy(ptr += sizeof(mlv_wbal_hdr_t), (uint8_t*)&(video->STYL), sizeof(mlv_styl_hdr_t));
    memcpy(ptr += sizeof(mlv_styl_hdr_t), (uint8_t*)&(video->WAVI), sizeof(mlv_wavi_hdr_t));
    memcpy(ptr += sizeof(mlv_wavi_hdr_t), (uint8_t*)&(video->DISO), sizeof(mlv_diso_hdr_t));
    memcpy(ptr += sizeof(mlv_diso_hdr_t), (uint8_t*)&(video->DARK), sizeof(mlv_dark_hdr_t));
    memcpy(ptr += sizeof(mlv_dark_hdr_t), (uint8_t*)&(video->camid), sizeof(camera_id_t));
    ptr += sizeof(camera_id_t);
    if(video->video_index)
    {
        memcpy(ptr, (uint8_t*)video->video_index, video_index_size);
        ptr += video_index_size;
    }
    if(video->audio_index)
    {
        memcpy(ptr, (uint8_t*)video->audio_index, audio_index_size);
        ptr += audio_index_size;
    }
    if(video->vers_index)
    {
        memcpy(ptr, (uint8_t*)video->vers_index, vers_index_size);
        ptr += vers_index_size;
    }

    /* open .MAPP file for writing */
    FILE* mappf = fopen(mapp_filename, "wb");
    if (!mappf)
    {
        DEBUG( printf("Could not open %s\n\n", mapp_filename); )
        free(mapp_buf);
        return 1;
    }

    /* write mapp buffer */
    if(fwrite(mapp_buf, mapp_buf_size, 1, mappf) != 1)
    {
        DEBUG( printf("\nCould not save header and metadata to %s\n", mapp_filename); )
        fclose(mappf);
        free(mapp_buf);
        return 1;
    }
    DEBUG( printf("\nHeader and metadata saved to %s\n", mapp_filename); )

    /* write mapp buffer */
    if(fwrite(video->audio_data, video->audio_size, 1, mappf) != 1)
    {
        DEBUG( printf("Could not save audio data to %s\n", mapp_filename); )
        fclose(mappf);
        free(mapp_buf);
        return 1;
    }
    DEBUG( printf("Audio data saved to %s\n", mapp_filename); )

    fclose(mappf);
    free(mapp_buf);
    return 0;
}

/* Load MLV App map file (.MAPP) */
static int load_mapp(mlvObject_t * video)
{
    int mapp_name_len = strlen(video->path);
    char * mapp_filename = alloca(mapp_name_len + 4);
    memset(mapp_filename, 0x00, mapp_name_len + 4);
    memcpy(mapp_filename, video->path, mapp_name_len);
    char * dot = strrchr(mapp_filename, '.');
    memcpy(dot, ".MAPP\0", 6);

    /* open .MAPP file for reading */
    FILE* mappf = fopen(mapp_filename, "rb");
    if (!mappf)
    {
        DEBUG( printf("Could not open %s\n\n", mapp_filename); )
        return 1;
    }

    /* Read .MAPP header */
    mapp_header_t mapp_header = { 0 };
    if ( fread(&mapp_header, sizeof(mapp_header_t), 1, mappf) != 1 )
    {
        DEBUG( printf("Could not read header from %s\n", mapp_filename); )
        goto mapp_error;
    }
    DEBUG( printf("Header loaded from %s\n", mapp_filename); )

    DEBUG(
        printf("Magic %s, Size %lu, Version %d, Total Blocks %d, Total VIDF %d, Total AUDF %d, Total VERS %d, Audio Size %lu, DF Offset %lu\n",
        mapp_header.fileMagic, mapp_header.mapp_size, mapp_header.mapp_version, mapp_header.block_num, mapp_header.video_frames,
        mapp_header.audio_frames, mapp_header.vers_blocks, mapp_header.audio_size, mapp_header.df_offset);
    )

    /* Check MAPP validity */
    if( memcmp(mapp_header.fileMagic, "MAPP", 4) != 0 )
    {
        DEBUG( printf("Not a valid MAPP file: %s\n", mapp_filename); )
        goto mapp_error;
    }
    /* Check MAPP version */
    if( mapp_header.mapp_version != MAPP_VERSION )
    {
        DEBUG( printf("Wrong MAPP version: %d. Please rebuild all MAPPs\n", mapp_header.mapp_version); )
        goto mapp_error;
    }

    uint64_t mark_pos = file_get_pos(mappf);
    file_set_pos(mappf, 0, SEEK_END);
    uint64_t mapp_file_size = file_get_pos(mappf);
    file_set_pos(mappf, mark_pos, SEEK_SET);
    if( mapp_header.mapp_size != mapp_file_size )
    {
        DEBUG( printf("MAPP file size is wrong: %s\n", mapp_filename); )
        goto mapp_error;
    }

    /* Read MLV block headers */
    int ret = 0;
    ret += fread(&(video->MLVI), sizeof(mlv_file_hdr_t), 1, mappf);
    ret += fread(&(video->RAWI), sizeof(mlv_rawi_hdr_t), 1, mappf);
    ret += fread(&(video->RAWC), sizeof(mlv_rawc_hdr_t), 1, mappf);
    ret += fread(&(video->IDNT), sizeof(mlv_idnt_hdr_t), 1, mappf);
    ret += fread(&(video->EXPO), sizeof(mlv_expo_hdr_t), 1, mappf);
    ret += fread(&(video->LENS), sizeof(mlv_lens_hdr_t), 1, mappf);
    ret += fread(&(video->ELNS), sizeof(mlv_elns_hdr_t), 1, mappf);
    ret += fread(&(video->RTCI), sizeof(mlv_rtci_hdr_t), 1, mappf);
    ret += fread(&(video->WBAL), sizeof(mlv_wbal_hdr_t), 1, mappf);
    ret += fread(&(video->STYL), sizeof(mlv_styl_hdr_t), 1, mappf);
    ret += fread(&(video->WAVI), sizeof(mlv_wavi_hdr_t), 1, mappf);
    ret += fread(&(video->DISO), sizeof(mlv_diso_hdr_t), 1, mappf);
    ret += fread(&(video->DARK), sizeof(mlv_dark_hdr_t), 1, mappf);
    ret += fread(&(video->camid), sizeof(camera_id_t), 1, mappf);
    if(ret != 14)
    {
        DEBUG( printf("ret = %d, could not read metadata from %s\n", ret, mapp_filename); )
        goto mapp_error;
    }
    DEBUG( printf("Metadata loaded from %s\n", mapp_filename); )

    /* Read video index */
    if(mapp_header.video_frames)
    {
        size_t video_index_size = mapp_header.video_frames * sizeof(frame_index_t);

        video->video_index = malloc(video_index_size);
        if(!video->video_index)
        {
            DEBUG( printf("Malloc error: video index\n"); )
            goto mapp_error;
        }

        if ( fread(video->video_index, video_index_size, 1, mappf) != 1 )
        {
            DEBUG( printf("Could not read video index from %s\n", mapp_filename); )
            goto mapp_error;
        }
        DEBUG( printf("Video index loaded from %s\n", mapp_filename); )
    }

    /* Read audio index */
    if(mapp_header.audio_frames)
    {
        size_t audio_index_size = mapp_header.audio_frames * sizeof(frame_index_t);

        video->audio_index = malloc(audio_index_size);
        if(!video->audio_index)
        {
            DEBUG( printf("Malloc error: audio index\n"); )
            goto mapp_error;
        }

        if ( fread(video->audio_index, audio_index_size, 1, mappf) != 1 )
        {
            DEBUG( printf("Could not read audio index from %s\n", mapp_filename); )
            goto mapp_error;
        }
        DEBUG( printf("Audio index loaded from %s\n", mapp_filename); )
    }

    /* Read vers index */
    if(mapp_header.vers_blocks)
    {
        size_t vers_index_size = mapp_header.vers_blocks * sizeof(frame_index_t);

        video->vers_index = malloc(vers_index_size);
        if(!video->vers_index)
        {
            DEBUG( printf("Malloc error: VERS index\n"); )
            goto mapp_error;
        }

        if ( fread(video->vers_index, vers_index_size, 1, mappf) != 1 )
        {
            DEBUG( printf("Could not read VERS index from %s\n", mapp_filename); )
            goto mapp_error;
        }
        DEBUG( printf("VERS index loaded from %s\n", mapp_filename); )
    }

    /* Read audio data */
    if(mapp_header.audio_size)
    {
        video->audio_buffer_size = mapp_header.audio_size;
        video->audio_size = mapp_header.audio_size;
        video->audio_data = malloc(mapp_header.audio_size);
        if ( fread(video->audio_data, mapp_header.audio_size, 1, mappf) != 1 )
        {
            DEBUG( printf("Could not read audio data from %s\n", mapp_filename); )
            goto mapp_error;
        }
        DEBUG( printf("Audio data loaded from %s\n", mapp_filename); )
    }

    /* Set video and audio frame counts */
    video->frames = mapp_header.video_frames;
    video->audios = mapp_header.audio_frames;

    /* Set some required values */
    video->block_num = mapp_header.block_num;
    video->dark_frame_offset = mapp_header.df_offset;
    video->vers_blocks = mapp_header.vers_blocks;

    DEBUG( printf("MAPP version %u loaded: %s\n", mapp_header.mapp_version, mapp_filename); )

    fclose(mappf);
    return 0;

mapp_error:

    if(video->video_index)
    {
        free(video->video_index);
        video->video_index = NULL;
    }
    if(video->audio_index)
    {
        free(video->audio_index);
        video->audio_index = NULL;
    }
    if(video->vers_index)
    {
        free(video->vers_index);
        video->vers_index = NULL;
    }
    if(video->audio_data)
    {
        free(video->audio_data);
        video->audio_data = NULL;
    }
    if(mappf) fclose(mappf);

    return 1;
}

#define LOG_THRESH 0.0061
#define LOG_STOPS  9.72

uint16_t log_encode_int(uint16_t x, uint16_t bl, uint16_t max, uint16_t max_value)
{
    double v;
    if (x <= bl)
        v = 0.0;
    else if (x >= max)
        v = 1.0;
    else
        v = (double)(x - bl) / (double)(max - bl);

    double encoded;
    if (v < LOG_THRESH) {
        double y_thresh = (log2(LOG_THRESH) - 1.0) / LOG_STOPS + 1.0;
        double slope_m  = 1.0 / (LOG_THRESH * LOG_STOPS * log(2.0));
        encoded = slope_m * (v - LOG_THRESH) + y_thresh;
    } else {
        encoded = (log2(v) - 1.0) / LOG_STOPS + 1.0;
    }

    double result = encoded * (double)max_value + 0.5;
    if (result < 0.0) result = 0.0;
    if (result > (double)max_value) result = (double)max_value;
    return (uint16_t)result;
}

uint16_t log_decode_int(uint16_t x, uint16_t bl, uint16_t max, uint16_t max_value)
{
    double y_thresh = (log2(LOG_THRESH) - 1.0) / LOG_STOPS + 1.0;
    double slope_m  = 1.0 / (LOG_THRESH * LOG_STOPS * log(2.0));

    if (x > max_value) x = max_value;
    double v = (double)x / (double)max_value;

    double decoded;
    if (v >= y_thresh)
        decoded = pow(2.0, (v - 1.0) * LOG_STOPS + 1.0);
    else
        decoded = LOG_THRESH + (v - y_thresh) / slope_m;

    double result = decoded * (double)(max - bl) + (double)bl + 0.5;
    if (result < 0.0) result = 0.0;
    if (result > 65535.0) result = 65535.0;
    return (uint16_t)result;
}

/* Save MLV headers */
int saveMlvHeaders(mlvObject_t * video, FILE * output_mlv, int export_audio, int export_mode, uint32_t frame_start, uint32_t frame_end, const char * version, char * error_message)
{
    int is_jp2k = export_mode == MLV_JP2K_LOW
        || export_mode == MLV_JP2K_MED
        || export_mode == MLV_JP2K_HIGH
        || export_mode == MLV_JP2K_VERYHIGH
        || export_mode == MLV_JP2K_VISULOSSLESS;
    int is_lossy = is_jp2k;

    if(export_mode == MLV_DF_INT && !video->DARK.blockType[0])
    {
        sprintf(error_message, "There is no internal darkframe in:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return 1;
    }
    else if((export_mode == MLV_LJ92) && isMlvCompressed(video))
    {
        sprintf(error_message, "MLV already compressed:  %s\nUse 'Fast Pass' instead", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return 1;
    }
    else if((export_mode == MLV_DECOMPRESS) && (!isMlvCompressed(video)))
    {
        sprintf(error_message, "MLV already uncompressed:  %s\nUse 'Fast Pass' instead", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return 1;
    }

    /* construct version info */
    char version_info[1024] = { 0 };
    char tms[64] = { 0 };
    char export_mode_str[32] = { 0 };
    char export_audio_str[8] = { 0 };
    time_t rawtm = time(NULL);
    struct tm *tm = localtime(&rawtm);
    strftime(tms, sizeof(tms), "%H:%M:%S %b %e %Y", tm);

    char * export_mode_strings[] = {
        "MLV_FAST_PASS",
        "MLV_LJ92",
        "MLV_DECOMPRESS",
        "MLV_AVERAGED_FRAME",
        "MLV_DF_INT",
        "MLV_JP2K_LOW",
        "MLV_JP2K_MED",
        "MLV_JP2K_HIGH",
        "MLV_JP2K_VERYHIGH",
        "MLV_JP2K_VISULOSSLESS"
    };

    strcat(export_mode_str, export_mode_strings[export_mode]);

    if(video->WAVI.blockType[0] && export_audio && (export_mode < MLV_AVERAGED_FRAME)) strcat(export_audio_str, "ON");
    else strcat(export_audio_str, "OFF");

    sprintf(version_info, "exported by MLV App version %s on %s; export mode: %s (audio: %s) ", version, tms, export_mode_str, export_audio_str);
    size_t vers_info_size = strlen(version_info) + 1;
    size_t vers_block_size = sizeof(mlv_vers_hdr_t) + vers_info_size;
    mlv_vers_hdr_t VERS_HEADER = { "VERS", vers_block_size, 0xFFFFFFFFFFFFFFFF, vers_info_size };

    /* calculate space needed for original VERS blocks */
    size_t orig_vers_blocks_size = 0;
    for (uint32_t i = 0; i < video->vers_blocks; ++i)
        orig_vers_blocks_size += sizeof(mlv_vers_hdr_t) + video->vers_index[i].frame_size;

    size_t mlv_headers_size = video->MLVI.blockSize + video->RAWI.blockSize + video->IDNT.blockSize +
                              video->EXPO.blockSize + video->LENS.blockSize + video->WBAL.blockSize +
                              video->RTCI.blockSize + vers_block_size + orig_vers_blocks_size;

    if(video->ELNS.blockType[0]) mlv_headers_size += video->ELNS.blockSize;
    if(video->RAWC.blockType[0]) mlv_headers_size += video->RAWC.blockSize;
    if(video->STYL.blockType[0]) mlv_headers_size += video->STYL.blockSize;
    if(video->DISO.blockType[0]) mlv_headers_size += video->DISO.blockSize;
    if(video->WAVI.blockType[0] && export_audio && !(export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT)) mlv_headers_size += video->WAVI.blockSize;
    if(video->INFO.blockType[0] && video->INFO_STRING[0]) mlv_headers_size += video->INFO.blockSize;
    if(video->llrawproc->dark_frame && export_mode < MLV_AVERAGED_FRAME) // if normal MLV export specified and dark frame exists
    {
        df_init(video);
        DEBUG( printf("Block Size = %u, DF Size = %u, Export Mode = %u, Filename = %s\n", video->llrawproc->dark_frame_hdr.blockSize, video->llrawproc->dark_frame_size, export_mode, video->llrawproc->dark_frame_filename); )
        DEBUG( printf("Headers Size += %u\n", video->llrawproc->dark_frame_hdr.blockSize); )
        mlv_headers_size += video->llrawproc->dark_frame_hdr.blockSize;
    }
    if (is_lossy)
    {
        mlv_headers_size += sizeof(mlv_curv_hdr_t) + 4096 * sizeof(uint16_t);
    }
    uint8_t * mlv_headers_buf = malloc(mlv_headers_size);
    if(!mlv_headers_buf)
    {
        sprintf(error_message, "Could not allocate memory for block headers");
        DEBUG( printf("\n%s\n", error_message); )
        return 1;
    }

    /* fill mlv_headers_buf */
    uint8_t * ptr = mlv_headers_buf;
    mlv_file_hdr_t output_mlvi = { 0 };
    memcpy(&output_mlvi, (uint8_t*)&(video->MLVI), sizeof(mlv_file_hdr_t));
    output_mlvi.fileNum = 0;
    output_mlvi.fileCount = 1;
    output_mlvi.videoFrameCount = (export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT) ? 1 : frame_end - frame_start + 1;
    output_mlvi.audioFrameCount = (!export_audio || (export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT)) ? 0 : 1;
    output_mlvi.videoClass = MLV_VIDEO_CLASS_RAW;
    if (export_mode == MLV_FAST_PASS) output_mlvi.videoClass = video->MLVI.videoClass;
    if (export_mode == MLV_LJ92) output_mlvi.videoClass |= MLV_VIDEO_CLASS_FLAG_LJ92;
    if (is_jp2k) output_mlvi.videoClass |= MLV_VIDEO_CLASS_FLAG_JPEG2K;
    output_mlvi.audioClass = (!export_audio || (export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT)) ? 0 : 1;
    if(export_mode == MLV_DF_INT)
    {
        output_mlvi.sourceFpsNom = video->DARK.sourceFpsNom;
        output_mlvi.sourceFpsDenom = video->DARK.sourceFpsDenom;
    }
    memcpy(ptr, &output_mlvi, sizeof(mlv_file_hdr_t));
    ptr += video->MLVI.blockSize;

    if(export_mode == MLV_DF_INT)
    {
        mlv_rawi_hdr_t output_rawi = { 0 };
        memcpy(&output_rawi, (uint8_t*)&(video->RAWI), sizeof(mlv_rawi_hdr_t));
        output_rawi.xRes = video->DARK.xRes;
        output_rawi.yRes = video->DARK.yRes;
        output_rawi.raw_info.width = video->DARK.rawWidth;
        output_rawi.raw_info.height = video->DARK.rawHeight;
        output_rawi.raw_info.bits_per_pixel = video->DARK.bits_per_pixel;
        output_rawi.raw_info.black_level = video->DARK.black_level;
        output_rawi.raw_info.white_level = video->DARK.white_level;
        memcpy(ptr, &output_rawi, sizeof(mlv_rawi_hdr_t));
    }
    else
    {
        memcpy(ptr, (uint8_t*)&(video->RAWI), sizeof(mlv_rawi_hdr_t));
    }
    ptr += video->RAWI.blockSize;

    if(video->RAWC.blockType[0])
    {
        if(export_mode == MLV_DF_INT)
        {
            mlv_rawc_hdr_t output_rawc = { 0 };
            memcpy(&output_rawc, (uint8_t*)&(video->RAWC), sizeof(mlv_rawc_hdr_t));
            output_rawc.binning_x = video->DARK.binning_x;
            output_rawc.skipping_x = video->DARK.skipping_x;
            output_rawc.binning_y = video->DARK.binning_y;
            output_rawc.skipping_y = video->DARK.skipping_y;
            memcpy(ptr, &output_rawc, sizeof(mlv_rawc_hdr_t));
        }
        else
        {
            memcpy(ptr, (uint8_t*)&(video->RAWC), sizeof(mlv_rawc_hdr_t));
        }
        ptr += video->RAWC.blockSize;
    }

    if(export_mode == MLV_DF_INT)
    {
        mlv_idnt_hdr_t output_idnt = { 0 };
        memcpy(&output_idnt, (uint8_t*)&(video->IDNT), sizeof(mlv_idnt_hdr_t));
        output_idnt.cameraModel = video->DARK.cameraModel;
        memcpy(ptr, &output_idnt, sizeof(mlv_idnt_hdr_t));
    }
    else
    {
        memcpy(ptr, (uint8_t*)&(video->IDNT), sizeof(mlv_idnt_hdr_t));
    }
    ptr += video->IDNT.blockSize;

    if(export_mode == MLV_DF_INT)
    {
        mlv_expo_hdr_t output_expo = { 0 };
        memcpy(&output_expo, (uint8_t*)&(video->EXPO), sizeof(mlv_expo_hdr_t));
        output_expo.isoMode = video->DARK.isoMode;
        output_expo.isoValue = video->DARK.isoValue;
        output_expo.isoAnalog = video->DARK.isoAnalog;
        output_expo.digitalGain = video->DARK.digitalGain;
        output_expo.shutterValue = video->DARK.shutterValue;
        memcpy(ptr, &output_expo, sizeof(mlv_expo_hdr_t));
    }
    else
    {
        memcpy(ptr, (uint8_t*)&(video->EXPO), sizeof(mlv_expo_hdr_t));
    }
    ptr += video->EXPO.blockSize;

    memcpy(ptr, (uint8_t*)&(video->LENS), sizeof(mlv_lens_hdr_t));
    ptr += video->LENS.blockSize;

    if(video->ELNS.blockType[0])
    {
        memcpy(ptr, (uint8_t*)&(video->ELNS), sizeof(mlv_elns_hdr_t));
        ptr += video->ELNS.blockSize;
    }

    memcpy(ptr, (uint8_t*)&(video->WBAL), sizeof(mlv_wbal_hdr_t));
    ptr += video->WBAL.blockSize;

    if(video->STYL.blockType[0])
    {
        memcpy(ptr, (uint8_t*)&(video->STYL), sizeof(mlv_styl_hdr_t));
        ptr += video->STYL.blockSize;
    }

    memcpy(ptr, (uint8_t*)&(video->RTCI), sizeof(mlv_rtci_hdr_t));
    ptr += video->RTCI.blockSize;

    if(video->INFO.blockType[0] && video->INFO_STRING[0])
    {
        memcpy(ptr, (uint8_t*)&(video->INFO), sizeof(mlv_info_hdr_t));
        ptr += sizeof(mlv_info_hdr_t);
        memcpy(ptr, (uint8_t*)&(video->INFO_STRING), strlen(video->INFO_STRING) + 1);
        ptr += (video->INFO.blockSize - sizeof(mlv_info_hdr_t) + strlen(video->INFO_STRING) + 1);
    }

    if(video->DISO.blockType[0])
    {
        memcpy(ptr, (uint8_t*)&(video->DISO), sizeof(mlv_diso_hdr_t));
        ptr += video->DISO.blockSize;
    }

    if(video->WAVI.blockType[0] && export_audio && !(export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT))
    {
        memcpy(ptr, (uint8_t*)&(video->WAVI), sizeof(mlv_wavi_hdr_t));
        ptr += video->WAVI.blockSize;
    }

    if(video->llrawproc->dark_frame && export_mode < MLV_AVERAGED_FRAME) // if normal MLV export specified and dark frame exists
    {
        memcpy(ptr, (uint8_t*)&(video->llrawproc->dark_frame_hdr), sizeof(mlv_dark_hdr_t));
        ptr += sizeof(mlv_dark_hdr_t);

        size_t df_packed_size = video->llrawproc->dark_frame_hdr.blockSize - sizeof(mlv_dark_hdr_t);
        uint8_t * df_packed = calloc(df_packed_size, 1);
        dng_pack_image_bits((uint16_t *)df_packed, video->llrawproc->dark_frame_data, video->llrawproc->dark_frame_hdr.xRes, video->llrawproc->dark_frame_hdr.yRes, video->llrawproc->dark_frame_hdr.bits_per_pixel, 0);
        memcpy(ptr, df_packed, df_packed_size);
        ptr += df_packed_size;
        DEBUG( printf("\nDARK block inserted\n"); )
    }

    if (is_lossy)
    {
        mlv_curv_hdr_t curv = {{'C', 'U', 'R', 'V'}, sizeof(mlv_curv_hdr_t) + 4096 * sizeof(uint16_t), 0};
        uint16_t decode_lut[4096];
        uint16_t max_value = 1 << (getMlvBitdepth(video));
        uint16_t bl = getMlvBlackLevel(video);
        for (int i = 0; i < 4096; i++) {
            decode_lut[i] = log_decode_int(i, bl, max_value, 4095);
        }
        memcpy(ptr, &curv, sizeof(mlv_curv_hdr_t));
        ptr += sizeof(mlv_curv_hdr_t);
        memcpy(ptr, &decode_lut, sizeof(decode_lut));
        ptr += sizeof(decode_lut);
    }

    memcpy(ptr, &VERS_HEADER, sizeof(mlv_vers_hdr_t));
    ptr += sizeof(mlv_vers_hdr_t);
    memcpy(ptr, version_info, vers_info_size);
    ptr += vers_info_size;

    /* read all VERS block headers */
    char orig_vers_block[1024] = { 0 };
    for (uint32_t i = 0; i < video->vers_blocks; ++i)
    {
        int chunk = video->vers_index[i].chunk_num;
        file_set_pos(video->file[chunk], video->vers_index[i].block_offset, SEEK_SET);
        uint32_t orig_vers_block_size = sizeof(mlv_vers_hdr_t) + video->vers_index[i].frame_size;
        if(fread(orig_vers_block, orig_vers_block_size, 1, video->file[chunk]) != 1)
        {
            sprintf(error_message, "Could not read VERS block header from:  %s", video->path);
            DEBUG( printf("\n%s\n", error_message); )
                    return 1;
        }
        else
        {
            memcpy(ptr, orig_vers_block, orig_vers_block_size);
            ptr += orig_vers_block_size;
        }
    }

    /* write mlv_headers_buf */
    if(fwrite(mlv_headers_buf, mlv_headers_size, 1, output_mlv) != 1)
    {
        sprintf(error_message, "Could not write MLV headers");
        DEBUG( printf("\n%s\n", error_message); )
        free(mlv_headers_buf);
        return 1;
    }

    DEBUG( printf("\nMLV headers saved\n"); )
    free(mlv_headers_buf);
    return 0;
}

/* Save video frame plus audio if available */
int saveMlvAVFrame(mlvObject_t * video, FILE * output_mlv, int export_audio, int export_mode, uint32_t frame_start, uint32_t frame_end, uint32_t frame_index, uint64_t * avg_buf, char * error_message)
{
    mlv_vidf_hdr_t vidf_hdr = { 0 };

    if(!video || !output_mlv || !error_message || video->RAWI.xRes == 0 || video->RAWI.yRes == 0
       || video->RAWI.raw_info.bits_per_pixel == 0
       || (size_t)video->RAWI.xRes > SIZE_MAX / (size_t)video->RAWI.yRes)
    {
        if(error_message) sprintf(error_message, "Invalid frame dimensions");
        return 1;
    }
    int write_ok = (export_mode == MLV_AVERAGED_FRAME) ? 0 : 1;
    const size_t pixel_count = (size_t)video->RAWI.xRes * (size_t)video->RAWI.yRes;
    if(pixel_count > SIZE_MAX / sizeof(uint16_t)
       || pixel_count > (SIZE_MAX / video->RAWI.raw_info.bits_per_pixel))
    {
        sprintf(error_message, "Frame dimensions exceed addressable memory");
        return 1;
    }
    const size_t frame_size_unpacked = pixel_count * sizeof(uint16_t);
    const size_t packed_bits = pixel_count * video->RAWI.raw_info.bits_per_pixel;
    const size_t frame_size_packed_size = (packed_bits + 7u) / 8u;
    size_t compression_capacity = 0;
    if(frame_size_packed_size > UINT32_MAX
       || !dng_lj92_output_capacity(video->RAWI.xRes, video->RAWI.yRes, &compression_capacity))
    {
        sprintf(error_message, "Frame dimensions exceed MLV/LJ92 limits");
        return 1;
    }
    const uint32_t frame_size_packed = (uint32_t)frame_size_packed_size;
    uint32_t max_frame_number = frame_end - frame_start + 1;

    int chunk = video->video_index[frame_index].chunk_num;
    uint32_t frame_size = video->video_index[frame_index].frame_size;
    uint64_t frame_offset = video->video_index[frame_index].frame_offset;
    uint64_t block_offset = video->video_index[frame_index].block_offset;

    uint8_t * block_buf = NULL;
    uint8_t * frame_buf = NULL;

    /* read VIDF block header */
    file_set_pos(video->file[chunk], block_offset, SEEK_SET);
    if(fread(&vidf_hdr, sizeof(mlv_vidf_hdr_t), 1, video->file[chunk]) != 1)
    {
        sprintf(error_message, "Could not read VIDF block header from:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return 1;
    }

    vidf_hdr.blockSize -= vidf_hdr.frameSpace;
    vidf_hdr.frameSpace = 0;

    /* ilia3101: Implementing compressed export - for compressed modes I have added a simpler
     * code path using this switch statement - read the frame using the simple frame reader utility,
     * and then encode. I have kept bouncyball's original efficient logic for uncompressed/lossless modes. */
    switch (export_mode)
    {
        case MLV_JP2K_LOW:
        case MLV_JP2K_MED:
        case MLV_JP2K_HIGH:
        case MLV_JP2K_VERYHIGH:
        case MLV_JP2K_VISULOSSLESS:
        {
            /* TODO ilia3101: maybe add some error handling to getMlvRawFrameUint16 */
            uint16_t * frame = malloc(pixel_count * sizeof(uint16_t));
            getMlvRawFrameUint16(video, frame_index, frame);

            uint16_t encode_lut[16384];
            uint16_t bl = getMlvBlackLevel(video);
            uint16_t max_value = 1 << getMlvBitdepth(video);
            for (int i = 0; i < 16384; i++) {
                encode_lut[i] = log_encode_int(i, bl, max_value, 4095);
            }

            for (uint32_t p = 0; p < pixel_count; p++) {
                frame[p] = encode_lut[frame[p]];
            }

            if (
                export_mode == MLV_JP2K_LOW
                ||export_mode == MLV_JP2K_MED
                ||export_mode == MLV_JP2K_HIGH
                ||export_mode == MLV_JP2K_VERYHIGH
                ||export_mode == MLV_JP2K_VISULOSSLESS
            ) {
                #ifdef ENABLE_JPEG2K
                void *encoders[4] = {NULL, NULL, NULL, NULL};
                int32_t *quarter_bufs[4] = {NULL, NULL, NULL, NULL};
                uint8_t *encoded_bufs[4] = {NULL, NULL, NULL, NULL};
                size_t enc_sizes[4] = {0, 0, 0, 0};
                int enc_ok = 1;
                uint32_t hw = video->RAWI.xRes / 2;
                uint32_t hh = video->RAWI.yRes / 2;
                size_t quarter_pixels = (size_t)((uint32_t)hw * (uint32_t)hh);
                size_t max_enc_size = quarter_pixels * 2;

                float jp2k_threshold = 0.0;
                if (export_mode == MLV_JP2K_LOW) {
                    jp2k_threshold = 0.010;
                } else if (export_mode == MLV_JP2K_MED) {
                    jp2k_threshold = 0.0065;
                } else if (export_mode == MLV_JP2K_HIGH) {
                    jp2k_threshold = 0.0045;
                } else if (export_mode == MLV_JP2K_VERYHIGH) {
                    jp2k_threshold = 0.0032;
                } else if (export_mode == MLV_JP2K_VISULOSSLESS) {
                    jp2k_threshold = 0.0015;
                }

                for (int c = 0; c < 4 && enc_ok; c++)
                {
                    encoders[c] = ojph_encoder_new();
                    if (!encoders[c]) { enc_ok = 0; break; }
                    ojph_encoder_set_image(encoders[c], hw, hh, 1, 12, 0);
                    ojph_encoder_set_decompositions(encoders[c], 5);
                    ojph_encoder_set_lossless(encoders[c], 0);
                    ojph_encoder_set_quantization(encoders[c], jp2k_threshold);

                    quarter_bufs[c] = (int32_t *)malloc(quarter_pixels * sizeof(int32_t));
                    encoded_bufs[c] = (uint8_t *)malloc(max_enc_size);
                    if (!quarter_bufs[c] || !encoded_bufs[c]) { enc_ok = 0; break; }
                }

                if (enc_ok)
                {
                    //TODO: does this parallelisation actually work?
                    #pragma omp parallel for num_threads(4)
                    for (int c = 0; c < 4; c++)
                    {
                        uint32_t x_off = (c & 1) ? 1 : 0;
                        uint32_t y_off = (c & 2) ? 1 : 0;
                        for (uint32_t y = 0; y < hh; y++)
                        {
                            uint32_t in_row = (y * 2 + y_off) * video->RAWI.xRes;
                            uint32_t out_row = y * hw;
                            for (uint32_t x = 0; x < hw; x++)
                            {
                                quarter_bufs[c][out_row + x] = (int32_t)frame[in_row + x * 2 + x_off];
                            }
                        }
                        enc_sizes[c] = ojph_encoder_encode_into(encoders[c], quarter_bufs[c],
                                                                  encoded_bufs[c], max_enc_size);
                    }
                }

                for (int c = 0; c < 4; c++) {
                    if (encoders[c]) ojph_encoder_free(encoders[c]);
                }

                if (enc_ok && enc_sizes[0] > 0 && enc_sizes[1] > 0 && enc_sizes[2] > 0 && enc_sizes[3] > 0)
                {
                    uint32_t offsets[4];
                    offsets[0] = 36;
                    offsets[1] = offsets[0] + (uint32_t)enc_sizes[0];
                    offsets[2] = offsets[1] + (uint32_t)enc_sizes[1];
                    offsets[3] = offsets[2] + (uint32_t)enc_sizes[2];
                    size_t total_size = (size_t)offsets[3] + enc_sizes[3];

                    vidf_hdr.blockSize = sizeof(mlv_vidf_hdr_t) + total_size;
                    vidf_hdr.frameSpace = 0;

                    block_buf = malloc(vidf_hdr.blockSize);
                    if (block_buf)
                    {
                        uint8_t *ptr = block_buf + sizeof(mlv_vidf_hdr_t);
                        memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));

                        uint32_t version = 1;
                        memcpy(ptr, &version, 4); ptr += 4;
                        memcpy(ptr, &offsets[0], 4); ptr += 4;
                        memcpy(ptr, &enc_sizes[0], 4); ptr += 4;
                        memcpy(ptr, &offsets[1], 4); ptr += 4;
                        memcpy(ptr, &enc_sizes[1], 4); ptr += 4;
                        memcpy(ptr, &offsets[2], 4); ptr += 4;
                        memcpy(ptr, &enc_sizes[2], 4); ptr += 4;
                        memcpy(ptr, &offsets[3], 4); ptr += 4;
                        memcpy(ptr, &enc_sizes[3], 4); ptr += 4;
                        memcpy(ptr, encoded_bufs[0], enc_sizes[0]); ptr += enc_sizes[0];
                        memcpy(ptr, encoded_bufs[1], enc_sizes[1]); ptr += enc_sizes[1];
                        memcpy(ptr, encoded_bufs[2], enc_sizes[2]); ptr += enc_sizes[2];
                        memcpy(ptr, encoded_bufs[3], enc_sizes[3]); ptr += enc_sizes[3];
                    }
                }
                else
                {
                    sprintf(error_message, "JPEG2000 encoding failed");
                    for (int c = 0; c < 4; c++)
                    {
                        if (quarter_bufs[c]) free(quarter_bufs[c]);
                        if (encoded_bufs[c]) free(encoded_bufs[c]);
                    }
                    free(frame);
                    return 1;
                }

                for (int c = 0; c < 4; c++)
                {
                    if (quarter_bufs[c]) free(quarter_bufs[c]);
                    if (encoded_bufs[c]) free(encoded_bufs[c]);
                }
                #else
                sprintf(error_message, "JPEG2000 export is not enabled in this build (ENABLE_JPEG2K)");
                free(frame);
                return 1;
                #endif
            }

            free(frame);
            break;
        }
        default:
        {
            size_t frame_capacity = frame_size_unpacked;
            if(compression_capacity > frame_capacity) frame_capacity = compression_capacity;
            if((size_t)frame_size > frame_capacity) frame_capacity = frame_size;
            if(export_mode == MLV_DF_INT && video->DARK.blockSize > sizeof(mlv_dark_hdr_t))
            {
                const size_t dark_payload = video->DARK.blockSize - sizeof(mlv_dark_hdr_t);
                if(dark_payload > frame_capacity) frame_capacity = dark_payload;
            }
            if(frame_capacity > UINT32_MAX - sizeof(mlv_vidf_hdr_t))
            {
                sprintf(error_message, "VIDF frame exceeds block-size limit");
                return 1;
            }
            /* Allocate for the largest legal packed, unpacked, compressed, or pass-through frame. */
            block_buf = calloc(sizeof(mlv_vidf_hdr_t) + frame_capacity, 1);
            if(!block_buf)
            {
                sprintf(error_message, "Could not allocate memory for VIDF block");
                DEBUG( printf("\n%s\n", error_message); )
                return 1;
            }
            /* for safety allocate max possible size buffer for image data, calculated for 16bits per pixel */
            frame_buf = calloc(frame_capacity, 1);
            if(!frame_buf)
            {
                sprintf(error_message, "Could not allocate memory for VIDF frame");
                DEBUG( printf("\n%s\n", error_message); )
                free(block_buf);
                return 1;
            }

            /* read frame buffer */
            file_set_pos(video->file[chunk], frame_offset, SEEK_SET);
            if(fread(frame_buf, frame_size, 1, video->file[chunk]) != 1)
            {
                sprintf(error_message, "Could not read VIDF image data from:  %s", video->path);
                DEBUG( printf("\n%s\n", error_message); )
                free(frame_buf);
                free(block_buf);
                return 1;
            }

            if(export_mode == MLV_DF_INT) // export internal dark frame as separate MLV
            {
                size_t df_packed_size = video->DARK.blockSize - sizeof(mlv_dark_hdr_t);
                /* read dark frame */
                file_set_pos(video->file[0], video->dark_frame_offset, SEEK_SET);
                if(fread(frame_buf, df_packed_size, 1, video->file[0]) != 1)
                {
                    sprintf(error_message, "Could not read DARK block image data from:  %s", video->path);
                    DEBUG( printf("\n%s\n", error_message); )
                    free(frame_buf);
                    free(block_buf);
                    return 1;
                }
                /* set blocksize and samplesAveraged to frameNumber */
                vidf_hdr.blockSize = video->DARK.blockSize;
                vidf_hdr.frameNumber = video->DARK.samplesAveraged;
                memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));
                memcpy((block_buf + sizeof(mlv_vidf_hdr_t)), frame_buf, df_packed_size);
            }
            else if(export_mode == MLV_AVERAGED_FRAME) // average all frames to one dark frame
            {
                uint16_t * frame_buf_unpacked = calloc(frame_size_unpacked, 1);
                if(!frame_buf_unpacked)
                {
                    sprintf(error_message, "Averaging: could not allocate memory for unpacked frame");
                    DEBUG( printf("\n%s\n", error_message); )
                    free(frame_buf);
                    free(block_buf);
                    return 1;
                }
                if(isMlvCompressed(video))
                {
                    /* Codec-neutral: isMlvCompressed() covers both LJ92 and
                     * JPEG2000, so decode through the reader rather than calling
                     * the LJ92 decoder directly. */
                    int ret = getMlvRawFrameUint16(video, frame_index, frame_buf_unpacked);
                    if(ret != 0)
                    {
                        sprintf(error_message, "Averaging: could not decompress frame #%u", frame_index);
                        DEBUG( printf("\n%s\n", error_message); )
                        free(frame_buf_unpacked);
                        free(frame_buf);
                        free(block_buf);
                        return ret;
                    }
                }
                else
                {
                    dng_unpack_image_bits(frame_buf_unpacked, (uint16_t*)frame_buf, video->RAWI.xRes, video->RAWI.yRes, video->RAWI.raw_info.bits_per_pixel);
                }
                for(size_t i = 0; i < pixel_count; i++)
                {
                    avg_buf[i] += frame_buf_unpacked[i];
                }

                if(frame_index == frame_end - 1)
                {
                    for(size_t i = 0; i < pixel_count; i++)
                    {
                        frame_buf_unpacked[i] = (avg_buf[i] + max_frame_number / 2) / max_frame_number;
                    }
                    dng_pack_image_bits((uint16_t *)frame_buf, frame_buf_unpacked, video->RAWI.xRes, video->RAWI.yRes, video->RAWI.raw_info.bits_per_pixel, 0);

                    vidf_hdr.frameNumber = max_frame_number;
                    vidf_hdr.blockSize = sizeof(mlv_vidf_hdr_t) + frame_size_packed;
                    memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));
                    memcpy((block_buf + sizeof(mlv_vidf_hdr_t)), frame_buf, frame_size_packed);
                    write_ok = 1;
                }

                free(frame_buf_unpacked);
            }
            else if((export_mode == MLV_LJ92) && (!isMlvLj92(video))) // compress MLV frame with LJ92 if specified
            {
                int ret = 0;
                size_t frame_size_compressed = 0;

                uint16_t * frame_buf_unpacked = calloc(frame_size_unpacked, 1);
                uint8_t * frame_buf_compressed = calloc(compression_capacity, 1);
                if(!frame_buf_unpacked || !frame_buf_compressed)
                {
                    sprintf(error_message, "Could not allocate memory for frame compressing");
                    DEBUG( printf("\n%s\n", error_message); )
                    free(frame_buf_unpacked);
                    free(frame_buf_compressed);
                    free(frame_buf);
                    free(block_buf);
                    return 1;
                }

                if(!ret)
                {
                    dng_unpack_image_bits(frame_buf_unpacked, (uint16_t*)frame_buf, video->RAWI.xRes, video->RAWI.yRes, video->RAWI.raw_info.bits_per_pixel);
                    ret = dng_compress_image((uint16_t*)frame_buf_compressed,
                                             compression_capacity,
                                             frame_buf_unpacked,
                                             &frame_size_compressed,
                                             video->RAWI.xRes,
                                             video->RAWI.yRes,
                                             video->RAWI.raw_info.bits_per_pixel);
                    if(ret == LJ92_ERROR_NONE)
                    {
                        vidf_hdr.blockSize = sizeof(mlv_vidf_hdr_t) + frame_size_compressed;
                        memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));
                        memcpy((block_buf + sizeof(mlv_vidf_hdr_t)), (uint8_t*)frame_buf_compressed, frame_size_compressed);
                    }
                    else
                    {
                        sprintf(error_message, "Could not compress frame: LJ92_ERROR %d", ret);
                        free(frame_buf_unpacked);
                        free(frame_buf_compressed);
                        free(frame_buf);
                        free(block_buf);
                        return ret ? ret : 1;
                    }
                }

                if(frame_buf_unpacked) free(frame_buf_unpacked);
                if(frame_buf_compressed) free(frame_buf_compressed);
            }
            else if((export_mode == MLV_DECOMPRESS) && isMlvCompressed(video)) // decompress MLV frame with LJ92 if specified
            {
                int ret = 0;

                uint16_t * frame_buf_unpacked = calloc(frame_size_unpacked, 1);
                if(!frame_buf_unpacked)
                {
                    sprintf(error_message, "Could not allocate memory for frame decompressing");
                    DEBUG( printf("\n%s\n", error_message); )
                    free(frame_buf);
                    free(block_buf);
                    return 1;
                }

                if(!ret)
                {
                    /* Codec-neutral decode, as upstream does here:
                     * isMlvCompressed() admits JPEG2000 as well as LJ92, so route
                     * through the reader instead of the LJ92-only decoder.
                     * Failures stay fail-closed - unlike upstream the fork
                     * does not fall back to emitting the original compressed
                     * frame, which would leave a mixed-videoClass output MLV. */
                    int decode_err = getMlvRawFrameUint16(video, frame_index, frame_buf_unpacked);
                    if(decode_err == 0)
                    {
                        dng_pack_image_bits((uint16_t*)frame_buf, frame_buf_unpacked, video->RAWI.xRes, video->RAWI.yRes, video->RAWI.raw_info.bits_per_pixel, 0);
                        vidf_hdr.blockSize = sizeof(mlv_vidf_hdr_t) + frame_size_packed;
                        memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));
                        memcpy((block_buf + sizeof(mlv_vidf_hdr_t)), frame_buf, frame_size_packed);
                    }
                    else
                    {
                        sprintf(error_message, "Could not decompress frame #%u", frame_index);
                        free(frame_buf_unpacked);
                        free(frame_buf);
                        free(block_buf);
                        return decode_err;
                    }
                }

                if(frame_buf_unpacked) free(frame_buf_unpacked);
            }
            else // pass through the original raw frame
            {
                memcpy(block_buf, &vidf_hdr, sizeof(mlv_vidf_hdr_t));
                memcpy((block_buf + sizeof(mlv_vidf_hdr_t)), frame_buf, frame_size);
            }
        }
    }

    /* if audio export is enabled */
    if(!(frame_start - frame_index - 1) && export_audio && !(export_mode == MLV_AVERAGED_FRAME || export_mode == MLV_DF_INT))
    {
        /* initialize AUDF header */
        mlv_audf_hdr_t audf_hdr = { { 'A','U','D','F' }, 0, 0, 0, 0 };

        /* Calculate the sum of audio sample sizes for all audio channels */
        uint64_t audio_sample_size = (uint64_t)getMlvAudioChannels(video)
            * (uint64_t)(getMlvAudioBitsPerSample(video) / 8);
        /* Calculate the audio alignement block size in bytes */
        uint16_t block_align = audio_sample_size * 1024;
        /* Calculate audio starting offset */
        uint64_t audio_start_offset = ( (uint64_t)( (double)(getMlvSampleRate(video) * audio_sample_size * (frame_start - 1)) / (double)getMlvFramerate(video) ) );
        /* Make sure start offset value is multiple of sum of all channel sample sizes */
        uint64_t audio_start_offset_aligned = audio_start_offset - (audio_start_offset % audio_sample_size);
        /* Calculate cut audio size */
        uint64_t cut_audio_size = (uint64_t)( (double)(getMlvSampleRate(video) * audio_sample_size * (frame_end - frame_start + 1)) / (double)getMlvFramerate(video) );
        /* check if cut_audio_size is multiple of 'block_align' bytes and not more than original audio data size */
        uint64_t cut_audio_size_aligned = MIN( (cut_audio_size - (cut_audio_size % block_align) + block_align), video->audio_size );
        /* make max audio size (uint32_t max value - 1) multiple of 'block_align' bytes */
        uint32_t max_audio_size = 0xFFFFFFFF - (0xFFFFFFFF % block_align);
        /* Not likely that audio size exeeds the 4.3gb but anyway check if cut_audio_size is more than uint32_t max value to not overflow blockSize variable */
        if(cut_audio_size_aligned > max_audio_size) cut_audio_size_aligned = max_audio_size;

        /* fill AUDF block header */
        audf_hdr.blockSize = sizeof(mlv_audf_hdr_t) + cut_audio_size_aligned;
        audf_hdr.timestamp = vidf_hdr.timestamp;

        /* write AUDF block header */
        if(fwrite(&audf_hdr, sizeof(mlv_audf_hdr_t), 1, output_mlv) != 1)
        {
            sprintf(error_message, "Could not write AUDF block header");
            DEBUG( printf("\n%s\n", error_message); )
            if (frame_buf != NULL) free(frame_buf);
            if (block_buf != NULL) free(block_buf);
            return 1;
        }

        /* write audio data */
        if(fwrite(video->audio_data + audio_start_offset_aligned, cut_audio_size_aligned, 1, output_mlv) != 1)
        {
            sprintf(error_message, "Could not write AUDF block audio data");
            DEBUG( printf("\n%s\n", error_message); )
            if (frame_buf != NULL) free(frame_buf);
            if (block_buf != NULL) free(block_buf);
            return 1;
        }
    }

    /* write mlvFrame */
    if(write_ok)
    {
        if(fwrite(block_buf, vidf_hdr.blockSize, 1, output_mlv) != 1)
        {
            sprintf(error_message, "Could not write video frame #%u", frame_index);
            DEBUG( printf("\n%s\n", error_message); )
            if (frame_buf != NULL) free(frame_buf);
            if (block_buf != NULL) free(block_buf);
            return 1;
        }
    }

    if (frame_buf != NULL) free(frame_buf);
    if (block_buf != NULL) free(block_buf);
    DEBUG( if( (export_mode == MLV_FAST_PASS) && (!isMlvCompressed(video)) ) printf("Saved video frame #%u\n", frame_index); )
    return 0;
}

/* Reads a mcraw file in to a mlv object(mlvObject_t struct)
 * only puts metadata in to the mlvObject_t, no debayering or bit unpacking
 */
int openMcrawClip(mlvObject_t * video, char * mcrawPath, int open_mode, char * error_message)
{
    video->path = malloc( strlen(mcrawPath) + 1 );
    memcpy(video->path, mcrawPath, strlen(mcrawPath));
    video->path[strlen(mcrawPath)] = 0x0;

    mr_ctx_t *ctx = mr_decoder_new(0);

    int res = mr_decoder_open(ctx, mcrawPath);

    if (res == 0) {
        res = mr_decoder_parse(ctx);
    }

    if (res != 0)
    {
        sprintf(error_message, "Could not open file:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return MLV_ERR_OPEN; // can not open file
    }

    FILE **files = malloc(sizeof(FILE*));
    files[0] = mr_get_file_handle(ctx);
    video->file = files;


    /* Mutexes for every file */
    video->main_file_mutex = calloc(sizeof(pthread_mutex_t), 1);
    pthread_mutex_init(video->main_file_mutex, NULL);

    /* In preview mode we don't need to waste time on audio loading from MAPP */
    if (open_mode != MLV_OPEN_PREVIEW)
    {
        // DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG
        //if (!load_mapp(video)) {
        //    goto short_cut;
        //}
    }

    int64_t video_index_max = mr_get_frame_count(ctx);

    if (open_mode == MLV_OPEN_PREVIEW) {
       video_index_max = 1;
    }

    video->compression_type = mr_get_compression_type(ctx);
    video->frames      = video_index_max;
    video->video_index = (frame_index_t *)calloc(video->frames, sizeof(frame_index_t));

    video->audios      = mr_get_audio_packet_count(ctx);
    video->audio_index = (frame_index_t *)calloc(video->audios, sizeof(frame_index_t));

    mr_buffer_offset_t *offsets = mr_get_index(ctx);

    for (int64_t i = 0; i < video->frames; i++)
    {
        video->video_index[i].frame_type   = 1;
        video->video_index[i].chunk_num    = 0;
        video->video_index[i].frame_size   = 0;
        video->video_index[i].frame_offset = offsets[i].offset + sizeof(mr_item_t);
        video->video_index[i].frame_number = i;
        video->video_index[i].frame_time   = offsets[i].timestamp / 1000000;   // ns to ms
        video->video_index[i].block_offset = offsets[i].offset;
    }

    mr_buffer_offset_t *audio_offsets = mr_get_audio_index(ctx);

    for (int64_t i = 0; i < video->audios; i++)
    {
        video->audio_index[i].frame_type   = 1;
        video->audio_index[i].chunk_num    = 0;
        video->audio_index[i].frame_size   = 0;
        video->audio_index[i].frame_offset = audio_offsets[i].offset + sizeof(mr_item_t);
        video->audio_index[i].frame_number = i;
        video->audio_index[i].frame_time   = audio_offsets[i].timestamp / 1000000;
        video->audio_index[i].block_offset = audio_offsets[i].offset;
    }

    memcpy(&video->WAVI.blockType, "WAVI", 4);
    video->WAVI.blockSize                 = sizeof(mlv_wavi_hdr_t);
    video->WAVI.format                    = 1;    // 1=Integer PCM, 6=alaw, 7=mulaw
    video->WAVI.bitsPerSample             = 16;
    video->WAVI.channels                  = mr_get_audio_channels(ctx);
    video->WAVI.samplingRate              = mr_get_audio_sample_rate(ctx);
    video->WAVI.bytesPerSecond            = video->WAVI.samplingRate * video->WAVI.channels * (video->WAVI.bitsPerSample / 8);
    video->WAVI.blockAlign                = video->WAVI.channels * (video->WAVI.bitsPerSample / 8);


    memcpy(&video->RAWI.blockType, "RAWI", 4);
    video->RAWI.blockSize                 = sizeof(mlv_rawi_hdr_t);
    video->RAWI.xRes                      = mr_get_width(ctx);
    video->RAWI.yRes                      = mr_get_height(ctx);
    video->RAWI.raw_info.active_area.x1   = 0;
    video->RAWI.raw_info.active_area.x2   = video->RAWI.xRes;
    video->RAWI.raw_info.active_area.y1   = 0;
    video->RAWI.raw_info.active_area.y2   = video->RAWI.yRes;

    video->RAWI.raw_info.bits_per_pixel   = mr_get_bits_per_pixel(ctx);
    video->RAWI.raw_info.black_level      = mr_get_black_level(ctx);
    video->RAWI.raw_info.white_level      = mr_get_white_level(ctx);
    video->RAWI.raw_info.cfa_pattern      = mr_get_cfa_pattern(ctx);
    video->RAWI.raw_info.exposure_bias[0] = 0;
    video->RAWI.raw_info.exposure_bias[1] = 0;


    double *matrix = mr_get_color_matrix1(ctx);
    for (int i = 0; i < 9; i++)
    {
        video->camid.ColorMatrix1[i * 2]     = (int)(matrix[i] * 10000.);
        video->camid.ColorMatrix1[i * 2 + 1] = 10000;
    }
    memcpy(video->RAWI.raw_info.color_matrix1, video->camid.ColorMatrix1, 18 * sizeof(int32_t));

    matrix = mr_get_color_matrix2(ctx);
    for (int i = 0; i < 9; i++)
    {
        video->camid.ColorMatrix2[i * 2]     = (int)(matrix[i] * 10000.);
        video->camid.ColorMatrix2[i * 2 + 1] = 10000;
    }

    matrix = mr_get_forward_matrix1(ctx);
    for (int i = 0; i < 9; i++)
    {
        video->camid.ForwardMatrix1[i * 2]     = (int)(matrix[i] * 10000.);
        video->camid.ForwardMatrix1[i * 2 + 1] = 10000;
    }

    matrix = mr_get_forward_matrix2(ctx);
    for (int i = 0; i < 9; i++)
    {
        video->camid.ForwardMatrix2[i * 2]     = (int)(matrix[i] * 10000.);
        video->camid.ForwardMatrix2[i * 2 + 1] = 10000;
    }

    video->MLVI.blockSize        = sizeof(mlv_file_hdr_t);
    video->MLVI.videoClass       = MLV_VIDEO_CLASS_FLAG_MCRAW;
    video->MLVI.videoFrameCount  = video_index_max;    // number of video frames in this file. set to 0 on start, updated when finished.

    mr_get_frame_rate(ctx, &video->MLVI.sourceFpsNom, &video->MLVI.sourceFpsDenom);
    video->MLVI.audioClass       = 1;                  // 0=none, 1=WAV
    video->MLVI.audioFrameCount  = 0;                  // number of audio frames in this file. set to 0 on start, updated when finished.

    memcpy(&video->WBAL.blockType, "WBAL", 4);
    video->WBAL.blockSize        = sizeof(mlv_wbal_hdr_t);
    video->WBAL.wb_mode          = 6;     // CUSTOM
    video->WBAL.timestamp        = 0;
    video->WBAL.kelvin           = 0;

    double *wb = mr_get_as_shot_neutral(ctx);
    video->WBAL.wbgain_r         = wb[0] * 1024;
    video->WBAL.wbgain_g         = wb[1] * 1024;
    video->WBAL.wbgain_b         = wb[2] * 1024;

    memcpy(&video->IDNT.blockType, "IDNT", 4);
    video->IDNT.blockSize        = sizeof(mlv_idnt_hdr_t);
    snprintf((char *)video->IDNT.cameraName, 31, "%s", mr_get_model(ctx));

    memcpy(&video->LENS.blockType, "LENS", 4);
    video->LENS.blockSize        = sizeof(mlv_lens_hdr_t);
    video->LENS.focalLength      = mr_get_focal_length(ctx);       // in mm
    video->LENS.aperture         = mr_get_aperture(ctx) * 100;     // f-number * 100

    memcpy(&video->EXPO.blockType, "EXPO", 4);
    video->EXPO.blockSize        = sizeof(mlv_lens_hdr_t);
    video->EXPO.isoValue         = mr_get_iso(ctx);
    video->EXPO.shutterValue     = mr_get_exposure_time(ctx) / 1000;

    time_t t = mr_get_timestamp(ctx) / 1000;
    struct tm tm = *localtime(&t);

    memcpy(&video->RTCI.blockType, "RTCI", 4);
    video->RTCI.blockSize        = sizeof(mlv_rtci_hdr_t);
    video->RTCI.tm_year          = tm.tm_year;
    video->RTCI.tm_mon           = tm.tm_mon;
    video->RTCI.tm_mday          = tm.tm_mday;
    video->RTCI.tm_hour          = tm.tm_hour;
    video->RTCI.tm_sec           = tm.tm_sec;

    /* Sort video and audio frames by time stamp */
    if (video->frames) {
        frame_index_sort(video->video_index, video->frames);
    }

    if (video->audios) {
        frame_index_sort(video->audio_index, video->audios);
    }

    /* Reads MLV audio into buffer (video->audio_data) and sync it,
     * set full audio buffer size (video->audio_buffer_size) and
     * aligned usable audio data size (video->audio_size) */
    readMlvAudioData(video);

    /* Save mapp file if this feature is on */
    if (open_mode == MLV_OPEN_MAPP)  {
       save_mapp(video);
    }

short_cut:

    /* Set imaginary lossless bit depth */
    setMlvLosslessBpp(video);

    video->llrawproc->diso_validity = DISO_INVALID;


    /* NON compressed frame size */
    video->frame_size = (getMlvHeight(video) * getMlvWidth(video) * getMlvBitdepth(video)) / 8;

    /* Calculate framerate */
    video->frame_rate = getMlvFramerateOrig(video);

    /* For frame cache */
    video->rgb_raw_frames = (uint16_t **)malloc( sizeof(uint16_t *) * video->frames );
    video->rgb_raw_current_frame_words = (uint64_t)getMlvWidth(video) * getMlvHeight(video) * 3;
    video->rgb_raw_current_frame = (uint16_t *)malloc( video->rgb_raw_current_frame_words * sizeof(uint16_t) );
    video->cached_frames = (uint8_t *)calloc( sizeof(uint8_t), video->frames );
    if (!video->rgb_raw_frames || !video->rgb_raw_current_frame || !video->cached_frames)
    {
        snprintf(error_message, 256, "Out of memory creating mcraw frame cache:  %.185s", video->path);
        return MLV_ERR_OPEN;
    }

    isMlvActive(video) = 5;
    const int cache_was_enabled = !mlvCacheShouldStop(video);
    if (cache_was_enabled && (open_mode != MLV_OPEN_PREVIEW))
    {
        mlvCacheSetStop(video, 1);
        setMlvRawCacheLimitMegaBytes(video, getMlvRawCacheLimitMegaBytes(video));
        mlvCacheSetStop(video, 0);
        if (!video->cache_memory_block)
        {
            snprintf(error_message, 256, "Out of memory applying mcraw frame cache policy:  %.176s", video->path);
            return MLV_ERR_OPEN;
        }
        for (int i = 0; i < video->cpu_cores; ++i)
        {
            add_mlv_cache_thread(video);
        }
    }

    return MLV_ERR_NONE;
}

/* Opens a FOLDER of CinemaDNG frames as a clip. Enumerates *.dng/*.DNG in
 * natural order, parses frame 0's TIFF/IFD for geometry + calibration, and
 * populates the mlvObject_t so the existing playback/render/processing path
 * works unchanged. Per-frame decode happens in getMlvRawFrameUint16Direct via
 * the isDngFolderLoaded() branch. Mirrors openMcrawClip(). */
int openDngFolderClip(mlvObject_t * video, char * dirPath, int open_mode, char * error_message)
{
    if (!video || !dirPath || !error_message) return MLV_ERR_INVALID;
    video->path = malloc( strlen(dirPath) + 1 );
    if (!video->path)
    {
        snprintf(error_message, 256, "Out of memory copying CinemaDNG folder path");
        return MLV_ERR_OPEN;
    }
    memcpy(video->path, dirPath, strlen(dirPath));
    video->path[strlen(dirPath)] = 0x0;

    /* Enumerate + parse frame 0. */
    dng_sequence_t * seq = (dng_sequence_t *)calloc(1, sizeof(dng_sequence_t));
    if (!seq)
    {
        snprintf(error_message, 256, "Out of memory opening DNG folder:  %.200s", video->path);
        return MLV_ERR_OPEN;
    }
    if (dng_sequence_open(dirPath, seq) != 0 || !seq->info.valid)
    {
        free(seq);
        snprintf(error_message, 256, "No readable CinemaDNG frames in folder:  %.195s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return MLV_ERR_OPEN;
    }
    video->dng_sequence = seq;

    const dng_frame_info_t * fi = &seq->info;
    if(fi->unique_camera_model[0] == '\0')
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256,
                 "CinemaDNG UniqueCameraModel is missing:  %.181s", video->path);
        return MLV_ERR_INVALID;
    }
    if(fi->camera_model[0] == '\0'
       && strlen(fi->unique_camera_model) >= sizeof(video->IDNT.cameraName))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256,
                 "CinemaDNG UniqueCameraModel is too long for Model fallback:  %.166s",
                 video->path);
        return MLV_ERR_INVALID;
    }
    size_t dng_pixels = 0;
    if (!mlvDngSequenceGeometryIsRepresentable(fi->width, fi->height,
                                                fi->bits_per_sample,
                                                &dng_pixels))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Unsupported CinemaDNG geometry in folder:  %.190s", video->path);
        return MLV_ERR_INVALID;
    }
    if (!mlvDngCfaPatternIsSupported(fi->cfa_pattern))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Unsupported CinemaDNG CFA pattern in folder:  %.187s", video->path);
        return MLV_ERR_INVALID;
    }
    if (fi->compression == DNG_READER_COMPRESSION_NONE
        && ((dng_pixels * (size_t)fi->bits_per_sample) & 15u) != 0)
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Unsupported packed CinemaDNG geometry in folder:  %.183s", video->path);
        return MLV_ERR_INVALID;
    }
    const uint32_t sample_max = fi->bits_per_sample == 16
        ? UINT16_MAX : ((UINT32_C(1) << fi->bits_per_sample) - 1u);
    const uint32_t effective_white = fi->white_level > 0
        ? (uint32_t)fi->white_level : sample_max;
    if (fi->black_level < 0 || (uint32_t)fi->black_level > sample_max
        || fi->white_level < 0
        || effective_white > sample_max
        || (uint32_t)fi->black_level >= effective_white)
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Invalid CinemaDNG sample range in folder:  %.188s", video->path);
        return MLV_ERR_INVALID;
    }
    uint32_t dng_wb_gains[3] = { 1024, 1024, 1024 };
    if (fi->has_as_shot_neutral
        && !mlvDngAsShotNeutralToWbGains(fi->as_shot_neutral, dng_wb_gains))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Invalid CinemaDNG white balance in folder:  %.188s", video->path);
        return MLV_ERR_INVALID;
    }
    uint32_t dng_sampling_x = 1u;
    uint32_t dng_sampling_y = 1u;
    if (fi->has_default_scale
        && !mlvDngDefaultScaleToSampling(fi->default_scale,
                                         &dng_sampling_x,
                                         &dng_sampling_y))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Unsupported CinemaDNG pixel aspect in folder:  %.185s", video->path);
        return MLV_ERR_INVALID;
    }
    if (!mlvDngAspectRatioIsSupported(dng_sampling_x, dng_sampling_y))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "CinemaDNG pixel aspect is not representable:  %.178s", video->path);
        return MLV_ERR_INVALID;
    }
    const int32_t zero_exposure[2] = { 0, 1 };
    const int32_t * baseline_exposure = fi->has_baseline_exposure
        ? fi->baseline_exposure : zero_exposure;
    const int32_t * baseline_offset = fi->has_baseline_exposure_offset
        ? fi->baseline_exposure_offset : zero_exposure;
    int32_t dng_exposure_bias[2] = { 0, 1 };
    if (!mlvDngBaselineExposureToBias(baseline_exposure,
                                      baseline_offset,
                                      dng_exposure_bias))
    {
        dng_sequence_free(seq);
        free(seq);
        video->dng_sequence = NULL;
        snprintf(error_message, 256, "Invalid CinemaDNG baseline exposure in folder:  %.180s", video->path);
        return MLV_ERR_INVALID;
    }

    /* In preview mode only the first frame is needed. */
    uint32_t frame_count = seq->count;
    if (open_mode == MLV_OPEN_PREVIEW) frame_count = 1;

    video->frames      = frame_count;
    /* The DNG-folder decode path does not use video_index/file[chunk], but a
     * non-NULL video_index keeps the rest of the object consistent (some code
     * indexes it for frame numbers / time). One entry per frame. */
    video->video_index = (frame_index_t *)calloc(video->frames ? video->frames : 1, sizeof(frame_index_t));
    if (!video->video_index)
    {
        snprintf(error_message, 256, "Out of memory indexing DNG folder:  %.194s", video->path);
        return MLV_ERR_OPEN;
    }
    for (uint32_t i = 0; i < video->frames; i++)
    {
        video->video_index[i].frame_type   = 1;
        video->video_index[i].chunk_num    = 0;
        video->video_index[i].frame_number = i;
        video->video_index[i].frame_time   = i;
    }

    /* No multi-file chunk model here, but allocate a 1-element file/mutex array
     * so any incidental file[0]/mutex[0] access stays valid. */
    video->filenum = 1;
    FILE ** dng_files = (FILE **)calloc(1, sizeof(FILE *));
    pthread_mutex_t * dng_mutex = (pthread_mutex_t *)calloc(1, sizeof(pthread_mutex_t));
    if (!dng_files || !dng_mutex)
    {
        free(dng_files);
        free(dng_mutex);
        snprintf(error_message, 256, "Out of memory preparing DNG folder handles:  %.183s", video->path);
        return MLV_ERR_OPEN;
    }
    if (pthread_mutex_init(dng_mutex, NULL) != 0)
    {
        free(dng_files);
        free(dng_mutex);
        snprintf(error_message, 256, "Could not initialize DNG folder lock:  %.192s", video->path);
        return MLV_ERR_OPEN;
    }
    video->file = dng_files;
    video->main_file_mutex = dng_mutex;

    /* RAWI geometry + calibration straight from the DNG IFD. */
    memcpy(&video->RAWI.blockType, "RAWI", 4);
    video->RAWI.blockSize                 = sizeof(mlv_rawi_hdr_t);
    video->RAWI.xRes                      = (uint16_t)fi->width;
    video->RAWI.yRes                      = (uint16_t)fi->height;
    video->RAWI.raw_info.bits_per_pixel   = fi->bits_per_sample;
    video->RAWI.raw_info.black_level      = fi->black_level;
    video->RAWI.raw_info.white_level      = fi->white_level ? fi->white_level : (int32_t)sample_max;
    video->RAWI.raw_info.cfa_pattern      = (int32_t)fi->cfa_pattern;
    video->RAWI.raw_info.exposure_bias[0] = dng_exposure_bias[0];
    video->RAWI.raw_info.exposure_bias[1] = dng_exposure_bias[1];

    /* Preserve DefaultScale through the source-of-truth RAWC aspect path used
     * by every playback/export/present route. */
    memcpy(&video->RAWC.blockType, "RAWC", 4);
    video->RAWC.blockSize = sizeof(mlv_rawc_hdr_t);
    video->RAWC.binning_x = (uint8_t)dng_sampling_x;
    video->RAWC.binning_y = (uint8_t)dng_sampling_y;
    video->RAWC.skipping_x = 0;
    video->RAWC.skipping_y = 0;

    /* Active area: use the DNG tag if present, else full frame. */
    if (fi->active_area[0] >= 0 && fi->active_area[1] >= 0
        && fi->active_area[2] > fi->active_area[0]
        && fi->active_area[3] > fi->active_area[1]
        && (uint32_t)fi->active_area[2] <= fi->height
        && (uint32_t)fi->active_area[3] <= fi->width)
    {
        video->RAWI.raw_info.active_area.y1 = fi->active_area[0];
        video->RAWI.raw_info.active_area.x1 = fi->active_area[1];
        video->RAWI.raw_info.active_area.y2 = fi->active_area[2];
        video->RAWI.raw_info.active_area.x2 = fi->active_area[3];
    }
    else
    {
        video->RAWI.raw_info.active_area.x1 = 0;
        video->RAWI.raw_info.active_area.y1 = 0;
        video->RAWI.raw_info.active_area.x2 = fi->width;
        video->RAWI.raw_info.active_area.y2 = fi->height;
    }
    if(fi->has_active_area)
        memcpy(video->RAWI.raw_info.dng_active_area, fi->active_area,
               sizeof(video->RAWI.raw_info.dng_active_area));
    else
    {
        video->RAWI.raw_info.dng_active_area[0] = 0;
        video->RAWI.raw_info.dng_active_area[1] = 0;
        video->RAWI.raw_info.dng_active_area[2] = (int32_t)fi->height;
        video->RAWI.raw_info.dng_active_area[3] = (int32_t)fi->width;
    }
    if(fi->has_default_crop_origin)
    {
        video->RAWI.raw_info.crop.origin[0] = (int32_t)fi->default_crop_origin[0];
        video->RAWI.raw_info.crop.origin[1] = (int32_t)fi->default_crop_origin[1];
        video->RAWI.raw_info.crop.size[0] = (int32_t)fi->default_crop_size[0];
        video->RAWI.raw_info.crop.size[1] = (int32_t)fi->default_crop_size[1];
    }
    else
    {
        /* DefaultCropOrigin is relative to ActiveArea, not to the full image. */
        video->RAWI.raw_info.crop.origin[0] = 0;
        video->RAWI.raw_info.crop.origin[1] = 0;
        video->RAWI.raw_info.crop.size[0] = video->RAWI.raw_info.active_area.x2
                                          - video->RAWI.raw_info.active_area.x1;
        video->RAWI.raw_info.crop.size[1] = video->RAWI.raw_info.active_area.y2
                                          - video->RAWI.raw_info.active_area.y1;
    }

    /* Color matrices: prefer the values baked into the DNG; fall back to the
     * default camid already loaded by initMlvObject() when absent. */
    if (fi->has_color_matrix1)
    {
        memcpy(video->camid.ColorMatrix1, fi->color_matrix1, 18 * sizeof(int32_t));
        memcpy(video->RAWI.raw_info.color_matrix1, fi->color_matrix1, 18 * sizeof(int32_t));
    }
    else
    {
        memcpy(video->RAWI.raw_info.color_matrix1, video->camid.ColorMatrix1, 18 * sizeof(int32_t));
    }
    if (fi->has_color_matrix2)
        memcpy(video->camid.ColorMatrix2, fi->color_matrix2, 18 * sizeof(int32_t));
    if (fi->has_forward_matrix1)
        memcpy(video->camid.ForwardMatrix1, fi->forward_matrix1, 18 * sizeof(int32_t));
    if (fi->has_forward_matrix2)
        memcpy(video->camid.ForwardMatrix2, fi->forward_matrix2, 18 * sizeof(int32_t));

    /* MLVI: mark as a DNG-folder source (in-memory only). */
    memcpy(&video->MLVI.fileMagic, "MLVI", 4);
    video->MLVI.blockSize        = sizeof(mlv_file_hdr_t);
    video->MLVI.videoClass       = MLV_VIDEO_CLASS_RAW | MLV_VIDEO_CLASS_FLAG_DNGSEQ;
    video->MLVI.videoFrameCount  = frame_count;
    video->MLVI.sourceFpsNom     = fi->has_frame_rate
        ? (uint32_t)fi->frame_rate[0] : 24u;
    video->MLVI.sourceFpsDenom   = fi->has_frame_rate
        ? (uint32_t)fi->frame_rate[1] : 1u;
    video->MLVI.audioClass       = 0;
    video->MLVI.audioFrameCount  = 0;

    /* White balance: preserve the repository's AsShotNeutral/WBAL round-trip
     * convention, scaled by 1024 just like the mcraw import path. */
    memcpy(&video->WBAL.blockType, "WBAL", 4);
    video->WBAL.blockSize        = sizeof(mlv_wbal_hdr_t);
    video->WBAL.wb_mode          = 6;     // CUSTOM
    video->WBAL.timestamp        = 0;
    video->WBAL.kelvin           = 0;
    video->WBAL.wbgain_r         = dng_wb_gains[0];
    video->WBAL.wbgain_g         = dng_wb_gains[1];
    video->WBAL.wbgain_b         = dng_wb_gains[2];

    memcpy(&video->IDNT.blockType, "IDNT", 4);
    video->IDNT.blockSize        = sizeof(mlv_idnt_hdr_t);
    snprintf((char *)video->IDNT.cameraName, sizeof(video->IDNT.cameraName),
             "%s", fi->camera_model[0]
                 ? fi->camera_model : fi->unique_camera_model);
    video->camid.cameraName[UNIQ] = fi->unique_camera_model;

    memcpy(&video->EXPO.blockType, "EXPO", 4);
    video->EXPO.blockSize        = sizeof(mlv_expo_hdr_t);
    video->EXPO.isoValue         = fi->has_iso ? fi->iso : 100;
    video->EXPO.shutterValue     = 1;     /* unknown; non-zero to avoid div-by-zero in UI */

    /* Save original levels for reset. */
    video->original_black_level  = getMlvBlackLevel(video);
    video->original_white_level  = getMlvWhiteLevel(video);

    /* Imaginary lossless bit depth + dual-iso validity (already-recon'd DNGs
     * are not dual-iso from MLV-App's perspective). */
    setMlvLosslessBpp(video);
    video->llrawproc->diso_validity = DISO_INVALID;

    /* NON compressed frame size. */
    const uint64_t dng_frame_bytes = ((uint64_t)dng_pixels * fi->bits_per_sample + 7u) / 8u;
    if (dng_frame_bytes > UINT32_MAX)
    {
        snprintf(error_message, 256, "CinemaDNG frame size is not representable:  %.180s", video->path);
        return MLV_ERR_INVALID;
    }
    video->frame_size = (uint32_t)dng_frame_bytes;

    /* Calculate framerate. */
    video->frame_rate = getMlvFramerateOrig(video);

    /* For frame cache. */
    if ((size_t)(video->frames ? video->frames : 1u) > SIZE_MAX / sizeof(uint16_t *)
        || dng_pixels > SIZE_MAX / 3u
        || dng_pixels * 3u > SIZE_MAX / sizeof(uint16_t))
    {
        snprintf(error_message, 256, "CinemaDNG cache size is not representable:  %.180s", video->path);
        return MLV_ERR_INVALID;
    }
    video->rgb_raw_frames = (uint16_t **)malloc( sizeof(uint16_t *) * (video->frames ? video->frames : 1) );
    video->rgb_raw_current_frame_words = (uint64_t)getMlvWidth(video) * getMlvHeight(video) * 3;
    video->rgb_raw_current_frame = (uint16_t *)malloc( video->rgb_raw_current_frame_words * sizeof(uint16_t) );
    video->cached_frames = (uint8_t *)calloc( sizeof(uint8_t), (video->frames ? video->frames : 1) );
    if (!video->rgb_raw_frames || !video->rgb_raw_current_frame || !video->cached_frames)
    {
        snprintf(error_message, 256, "Out of memory creating DNG frame cache:  %.187s", video->path);
        return MLV_ERR_OPEN;
    }

    isMlvActive(video) = 1;
    const int cache_was_enabled = !mlvCacheShouldStop(video);
    if (cache_was_enabled && (open_mode != MLV_OPEN_PREVIEW))
    {
        mlvCacheSetStop(video, 1);
        setMlvRawCacheLimitMegaBytes(video, getMlvRawCacheLimitMegaBytes(video));
        mlvCacheSetStop(video, 0);
        if (!video->cache_memory_block)
        {
            snprintf(error_message, 256, "Out of memory applying DNG frame cache policy:  %.178s", video->path);
            return MLV_ERR_OPEN;
        }
        for (int i = 0; i < video->cpu_cores; ++i)
        {
            add_mlv_cache_thread(video);
        }
    }

    return MLV_ERR_NONE;
}

/* Reads an MLV file in to a mlv object(mlvObject_t struct)
 * only puts metadata in to the mlvObject_t,
 * no debayering or bit unpacking */
int openMlvClip(mlvObject_t * video, char * mlvPath, int open_mode, char * error_message)
{
    video->path = malloc( strlen(mlvPath) + 1 );
    memcpy(video->path, mlvPath, strlen(mlvPath));
    video->path[strlen(mlvPath)] = 0x0;
    video->file = load_all_chunks(mlvPath, &video->filenum);
    if(!video->file)
    {
        sprintf(error_message, "Could not open file:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        return MLV_ERR_OPEN; // can not open file
    }

    /* Mutexes for every file */
    video->main_file_mutex = calloc(sizeof(pthread_mutex_t), video->filenum);
    for (int i = 0; i < video->filenum; ++i)
    {
        pthread_mutex_init(video->main_file_mutex + i, NULL);
    }

    /* In preview mode we don't need to waste time on audio loading from MAPP */
    if(open_mode != MLV_OPEN_PREVIEW)
    {
        if(!load_mapp(video)) goto short_cut;
    }

    uint64_t block_num = 0; /* Number of blocks in file */
    mlv_hdr_t block_header; /* Basic MLV block header */
    uint64_t video_frames = 0; /* Number of frames in video */
    uint64_t audio_frames = 0; /* Number of audio blocks in video */
    uint32_t vers_blocks = 0; /* Number of VERS blocks in MLV */
    uint64_t video_index_max = 0; /* initial size of frame index */
    uint64_t audio_index_max = 0; /* initial size of audio index */
    uint32_t vers_index_max = 0; /* initial size of VERS index */
    int mlvi_read = 0; /* Flips to 1 if 1st chunk MLVI block was read */
    int rtci_read = 0; /* Flips to 1 if 1st RTCI block was read */
    int lens_read = 0; /* Flips to 1 if 1st LENS block was read */
    int elns_read = 0; /* Flips to 1 if 1st ELNS block was read */
    int wbal_read = 0; /* Flips to 1 if 1st WBAL block was read */
    int styl_read = 0; /* Flips to 1 if 1st STYL block was read */
    int curv_read = 0; /* Flips to 1 if 1st CURV block was read */
    int fread_err = 1;

    for(int i = 0; i < video->filenum; i++)
    {
        /* Getting size of file in bytes */
        file_set_pos(video->file[i], 0, SEEK_END);
        uint64_t file_size = file_get_pos(video->file[i]);
        if ( !file_size )
        {
            sprintf(error_message, "Zero byte size file:  %s", video->path);
            DEBUG( printf("\n%s\n", error_message); )
            --video->filenum;
            return MLV_ERR_INVALID;
        }
        file_set_pos(video->file[i], 0, SEEK_SET); /* Start of file */

        /* Read file header */
        if ( fread(&block_header, sizeof(mlv_hdr_t), 1, video->file[i]) != 1 )
        {
            sprintf(error_message, "File is too short to be a valid MLV:  %s", video->path);
            DEBUG( printf("\n%s\n", error_message); )
            --video->filenum;
            return MLV_ERR_INVALID;
        }
        file_set_pos(video->file[i], 0, SEEK_SET); /* Start of file */

        if ( memcmp(block_header.blockType, "MLVI", 4) == 0 )
        {
            if( !mlvi_read )
            {
                fread_err &= fread(&video->MLVI, sizeof(mlv_file_hdr_t), 1, video->file[i]);
                mlvi_read = 1; // read MLVI only for first chunk
            }
        }
        else
        {
            sprintf(error_message, "File header is missing, invalid MLV:  %s", video->path);
            DEBUG( printf("\n%s\n", error_message); )
            --video->filenum;
            return MLV_ERR_INVALID;
        }

        while ( file_get_pos(video->file[i]) < file_size ) /* Check if were at end of file yet */
        {
            /* Record position to go back to it later if block is read */
            uint64_t block_start = file_get_pos(video->file[i]);
            /* Read block header */
            fread_err &= fread(&block_header, sizeof(mlv_hdr_t), 1, video->file[i]);
            if(block_header.blockSize < sizeof(mlv_hdr_t))
            {
                sprintf(error_message, "Invalid blockSize '%u', corrupted file:  %s", block_header.blockSize, video->path);
                DEBUG( printf("\n%s\n", error_message); )
                --video->filenum;
                return MLV_ERR_INVALID;
            }

            /* Next block location */
            uint64_t next_block = (uint64_t)block_start + (uint64_t)block_header.blockSize;
            /* Go back to start of block for next bit */
            file_set_pos(video->file[i], block_start, SEEK_SET);

            /* Now check what kind of block it is and read it in to the mlv object */
            if ( memcmp(block_header.blockType, "NULL", 4) == 0 || memcmp(block_header.blockType, "BKUP", 4) == 0)
            {
                /* do nothing, skip this block */
            }
            else if ( memcmp(block_header.blockType, "VIDF", 4) == 0 )
            {
                fread_err &= fread(&video->VIDF, sizeof(mlv_vidf_hdr_t), 1, video->file[i]);

                DEBUG( printf("video frame %i | chunk %i | size %lu | offset %lu | time %lu\n",
                               video->VIDF.frameNumber, i, video->VIDF.blockSize - sizeof(mlv_vidf_hdr_t) - video->VIDF.frameSpace,
                               block_start + video->VIDF.frameSpace, video->VIDF.timestamp); )

                /* Dynamically resize the frame index buffer */
                if(!video_index_max)
                {
                    video_index_max = 128;
                    video->video_index = (frame_index_t *)calloc(video_index_max, sizeof(frame_index_t));
                }
                else if(video_frames >= video_index_max - 1)
                {
                    uint64_t video_index_new_size = video_index_max * 2;
                    frame_index_t * video_index_new = (frame_index_t *)calloc(video_index_new_size, sizeof(frame_index_t));
                    memcpy(video_index_new, video->video_index, video_index_max * sizeof(frame_index_t));
                    free(video->video_index);
                    video->video_index = video_index_new;
                    video_index_max = video_index_new_size;
                }

                /* Fill frame index */
                video->video_index[video_frames].frame_type = 1;
                video->video_index[video_frames].chunk_num = i;
                video->video_index[video_frames].frame_size = video->VIDF.blockSize - sizeof(mlv_vidf_hdr_t) - video->VIDF.frameSpace;
                video->video_index[video_frames].frame_offset = file_get_pos(video->file[i]) + video->VIDF.frameSpace;
                video->video_index[video_frames].frame_number = video->VIDF.frameNumber;
                video->video_index[video_frames].frame_time = video->VIDF.timestamp;
                video->video_index[video_frames].block_offset = block_start;

                /* Count actual video frames */
                video_frames++;

                /* In preview mode exit loop after first videf read */
                if(open_mode == MLV_OPEN_PREVIEW)
                {
                    video->frames = video_frames;
                    video->audios = audio_frames;
                    goto preview_out;
                }
            }
            else if ( memcmp(block_header.blockType, "AUDF", 4) == 0 )
            {
                fread_err &= fread(&video->AUDF, sizeof(mlv_audf_hdr_t), 1, video->file[i]);

                DEBUG( printf("audio frame %i | chunk %i | size %lu | offset %lu | time %lu\n",
                               video->AUDF.frameNumber, i, video->AUDF.blockSize - sizeof(mlv_audf_hdr_t) - video->AUDF.frameSpace,
                               block_start + video->AUDF.frameSpace, video->AUDF.timestamp); )

                /* Dynamically resize the audio index buffer */
                if(!audio_index_max)
                {
                    audio_index_max = 32;
                    video->audio_index = (frame_index_t *)malloc(sizeof(frame_index_t) * audio_index_max);
                }
                else if(audio_frames >= audio_index_max - 1)
                {
                    uint64_t audio_index_new_size = audio_index_max * 2;
                    frame_index_t * audio_index_new = (frame_index_t *)calloc(audio_index_new_size, sizeof(frame_index_t));
                    memcpy(audio_index_new, video->audio_index, audio_index_max * sizeof(frame_index_t));
                    free(video->audio_index);
                    video->audio_index = audio_index_new;
                    audio_index_max = audio_index_new_size;
                }

                /* Fill audio index */
                video->audio_index[audio_frames].frame_type = 2;
                video->audio_index[audio_frames].chunk_num = i;
                video->audio_index[audio_frames].frame_size = video->AUDF.blockSize - sizeof(mlv_audf_hdr_t) - video->AUDF.frameSpace;
                video->audio_index[audio_frames].frame_offset = file_get_pos(video->file[i]) + video->AUDF.frameSpace;
                video->audio_index[audio_frames].frame_number = video->AUDF.frameNumber;
                video->audio_index[audio_frames].frame_time = video->AUDF.timestamp;
                video->audio_index[audio_frames].block_offset = block_start;

                /* Count actual audio frames */
                audio_frames++;
            }
            else if ( memcmp(block_header.blockType, "RAWI", 4) == 0 )
            {
                fread_err &= fread(&video->RAWI, sizeof(mlv_rawi_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "RAWC", 4) == 0 )
            {
                fread_err &= fread(&video->RAWC, sizeof(mlv_rawc_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "WAVI", 4) == 0 )
            {
                fread_err &= fread(&video->WAVI, sizeof(mlv_wavi_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "EXPO", 4) == 0 )
            {
                fread_err &= fread(&video->EXPO, sizeof(mlv_expo_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "LENS", 4) == 0 )
            {
                if( !lens_read )
                {
                    fread_err &= fread(&video->LENS, sizeof(mlv_lens_hdr_t), 1, video->file[i]);
                    lens_read = 1; //read only first one
                    //Terminate string, if it isn't terminated.
                    for( int n = 0; n < 32; n++ )
                    {
                        if( video->LENS.lensName[n] == '\0' ) break;
                        if( n == 31 ) video->LENS.lensName[n] = '\0';
                    }
                }
            }
            else if ( memcmp(block_header.blockType, "ELNS", 4) == 0 )
            {
                if( !elns_read )
                {
                    fread_err &= fread(&video->ELNS, sizeof(mlv_elns_hdr_t), 1, video->file[i]);
                    elns_read = 1; //read only first one
                }
            }
            else if ( memcmp(block_header.blockType, "WBAL", 4) == 0 )
            {
                if( !wbal_read )
                {
                    fread_err &= fread(&video->WBAL, sizeof(mlv_wbal_hdr_t), 1, video->file[i]);
                    wbal_read = 1; //read only first one
                }
            }
            else if ( memcmp(block_header.blockType, "STYL", 4) == 0 )
            {
                if( !styl_read )
                {
                    fread_err &= fread(&video->STYL, sizeof(mlv_styl_hdr_t), 1, video->file[i]);
                    styl_read = 1; //read only first one
                }
            }
            else if ( memcmp(block_header.blockType, "RTCI", 4) == 0 )
            {
                if( !rtci_read )
                {
                    fread_err &= fread(&video->RTCI, sizeof(mlv_rtci_hdr_t), 1, video->file[i]);
                    rtci_read = 1; //read only first one
                }
            }
            else if ( memcmp(block_header.blockType, "IDNT", 4) == 0 )
            {
                fread_err &= fread(&video->IDNT, sizeof(mlv_idnt_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "INFO", 4) == 0 )
            {
                fread_err &= fread(&video->INFO, sizeof(mlv_info_hdr_t), 1, video->file[i]);
                if(video->INFO.blockSize > sizeof(mlv_info_hdr_t))
                {
                    fread_err &= fread(&video->INFO_STRING, video->INFO.blockSize - sizeof(mlv_info_hdr_t), 1, video->file[i]);
                }
            }
            else if ( memcmp(block_header.blockType, "DISO", 4) == 0 )
            {
                fread_err &= fread(&video->DISO, sizeof(mlv_diso_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "MARK", 4) == 0 )
            {
                /* do nothing atm */
                //fread(&video->MARK, sizeof(mlv_mark_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "ELVL", 4) == 0 )
            {
                /* do nothing atm */
                //fread(&video->ELVL, sizeof(mlv_elvl_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "DEBG", 4) == 0 )
            {
                /* do nothing atm */
                //fread(&video->DEBG, sizeof(mlv_debg_hdr_t), 1, video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "VERS", 4) == 0 )
            {
                /* Find all VERS blocks and make index for them */
                fread_err &= fread(&video->VERS, sizeof(mlv_vers_hdr_t), 1, video->file[i]);

                DEBUG( printf("VERS blocknum %i | chunk %i | size %lu | offset %lu | time %lu\n",
                               vers_blocks, i, video->VERS.blockSize - sizeof(mlv_vers_hdr_t),
                               block_start, video->VERS.timestamp); )

                /* Dynamically resize the index buffer */
                if(!vers_index_max)
                {
                    vers_index_max = 128;
                    video->vers_index = (frame_index_t *)calloc(vers_index_max, sizeof(frame_index_t));
                }
                else if(vers_blocks >= vers_index_max - 1)
                {
                    uint64_t vers_index_new_size = vers_index_max * 2;
                    frame_index_t * vers_index_new = (frame_index_t *)calloc(vers_index_new_size, sizeof(frame_index_t));
                    memcpy(vers_index_new, video->vers_index, vers_index_max * sizeof(frame_index_t));
                    free(video->vers_index);
                    video->vers_index = vers_index_new;
                    vers_index_max = vers_index_new_size;
                }

                /* Fill frame index */
                video->vers_index[vers_blocks].frame_type = 3;
                video->vers_index[vers_blocks].chunk_num = i;
                video->vers_index[vers_blocks].frame_size = video->VERS.blockSize - sizeof(mlv_vers_hdr_t);
                video->vers_index[vers_blocks].frame_offset = file_get_pos(video->file[i]);
                video->vers_index[vers_blocks].frame_number = vers_blocks;
                video->vers_index[vers_blocks].frame_time = video->VERS.timestamp;
                video->vers_index[vers_blocks].block_offset = block_start;

                /* Count actual VERS blocks */
                vers_blocks++;
            }
            else if ( memcmp(block_header.blockType, "DARK", 4) == 0 )
            {
                fread_err &= fread(&video->DARK, sizeof(mlv_dark_hdr_t), 1, video->file[i]);
                video->dark_frame_offset = file_get_pos(video->file[i]);
            }
            else if ( memcmp(block_header.blockType, "CURV", 4) == 0 )
            {
                if( !curv_read )
                {
                    mlv_curv_hdr_t cur_hdr;
                    fread_err &= fread(&cur_hdr, sizeof(mlv_curv_hdr_t), 1, video->file[i]);
                    uint32_t lut_entries = 0;
                    if(!mlvCurvLutEntryCount(cur_hdr.blockSize, &lut_entries))
                    {
                        sprintf(error_message, "CURV block declares an out-of-range curve size (%" PRIu32 " bytes):  %s", cur_hdr.blockSize, video->path);
                        DEBUG( printf("\n%s\n", error_message); )
                        --video->filenum;
                        return MLV_ERR_CORRUPTED;
                    }
                    video->linearise_lut = (uint16_t *)calloc(MLV_LINEARISE_LUT_ENTRIES, sizeof(uint16_t));
                    if(!video->linearise_lut)
                    {
                        sprintf(error_message, "Could not allocate memory for the CURV linearisation table:  %s", video->path);
                        DEBUG( printf("\n%s\n", error_message); )
                        --video->filenum;
                        return MLV_ERR_IO;
                    }
                    /* lut_entries is bounded by the allocation above, so this
                     * read can never run past the table. */
                    fread_err &= fread(video->linearise_lut, lut_entries * sizeof(uint16_t), 1, video->file[i]);
                    curv_read = 1;
                }
            }
            else
            {
                /* block name is wrong, so try to brute force the position of next valid block */
                if(!seek_to_next_known_block(video->file[i]))
                {
                    char block_type[5] = { 0 };
                    memcpy(block_type, block_header.blockType, 4);
                    sprintf(error_message, "Unknown blockType '%s' or corrupted file:  %s", block_type, video->path);
                    DEBUG( printf("\n%s\n", error_message); )
                            --video->filenum;
                    return MLV_ERR_CORRUPTED;
                }
                continue;
            }

            /* Printing stuff for fun */
            //DEBUG( printf("Block #%4i  |  %.4s  |%9i Bytes\n", block_num, block_header.blockType, block_header.blockSize); )

            /* Move to next block */
            file_set_pos(video->file[i], next_block, SEEK_SET);

            block_num++;
        }
    }

    /* Return with error if no video frames found */
    if(!fread_err)
    {
        sprintf(error_message, "File read error:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        --video->filenum;
        return MLV_ERR_IO;
    }
    /* Return with error if no video frames found */
    if(!video_frames)
    {
        sprintf(error_message, "No video frames found in:  %s", video->path);
        DEBUG( printf("\n%s\n", error_message); )
        --video->filenum;
        return MLV_ERR_INVALID;
    }

    /* Set total block amount in mlv */
    video->block_num = block_num;

    /* Sort video and audio frames by time stamp */
    if(video_frames) frame_index_sort(video->video_index, video_frames);
    if(audio_frames) frame_index_sort(video->audio_index, audio_frames);

    /* Set frame count in video object */
    video->frames = video_frames;
    /* Set audio count in video object */
    video->audios = audio_frames;
    /* Set VERS block count in video object */
    video->vers_blocks = vers_blocks;

    /* Reads MLV audio into buffer (video->audio_data) and sync it,
     * set full audio buffer size (video->audio_buffer_size) and
     * aligned usable audio data size (video->audio_size) */
    readMlvAudioData(video);

    /* Save mapp file if this feature is on */
    if(open_mode == MLV_OPEN_MAPP) save_mapp(video);

short_cut:

    /* Set imaginary lossless bit depth */
    setMlvLosslessBpp(video);
    /* Check and set dual iso validity */
    llrpSetDualIsoValidity(video, 0);

preview_out:

    /* NON compressed frame size */
    video->frame_size = (getMlvHeight(video) * getMlvWidth(video) * getMlvBitdepth(video)) / 8;
    /* Calculate framerate */
    video->frame_rate = getMlvFramerateOrig(video);

    /* For frame cache */
    video->rgb_raw_frames = (uint16_t **)malloc( sizeof(uint16_t *) * video->frames );
    video->rgb_raw_current_frame_words = (uint64_t)getMlvWidth(video) * getMlvHeight(video) * 3;
    video->rgb_raw_current_frame = (uint16_t *)malloc( video->rgb_raw_current_frame_words * sizeof(uint16_t) );
    video->cached_frames = (uint8_t *)calloc( sizeof(uint8_t), video->frames );
    if (!video->rgb_raw_frames || !video->rgb_raw_current_frame || !video->cached_frames)
    {
        snprintf(error_message, 256, "Out of memory creating MLV frame cache:  %.188s", video->path);
        return MLV_ERR_OPEN;
    }

    isMlvActive(video) = 1;
    const int cache_was_enabled = !mlvCacheShouldStop(video);
    if (cache_was_enabled && (open_mode != MLV_OPEN_PREVIEW))
    {
        mlvCacheSetStop(video, 1);
        setMlvRawCacheLimitMegaBytes(video, getMlvRawCacheLimitMegaBytes(video));
        mlvCacheSetStop(video, 0);
        if (!video->cache_memory_block)
        {
            snprintf(error_message, 256, "Out of memory applying MLV frame cache policy:  %.178s", video->path);
            return MLV_ERR_OPEN;
        }
        for (int i = 0; i < video->cpu_cores; ++i)
        {
            add_mlv_cache_thread(video);
        }
    }

    return MLV_ERR_NONE;
}

void setMlvLosslessBpp(mlvObject_t * video)
{
    /* Calculate imaginary bit depth for restricted lossledd raw data */
    video->lossless_bpp = ceil( log2( getMlvWhiteLevel(video) - getMlvBlackLevel(video) ) );
}

/* Get image aspect ratio according to RAWC block info, calculating from binnin + skipping values.
   Returns aspect ratio or 0 in case if RAWC block is not present in MLV file */
float getMlvAspectRatio(mlvObject_t * video)
{
    if(video->RAWC.blockType[0])
    {
        int sampling_x = video->RAWC.binning_x + video->RAWC.skipping_x;
        int sampling_y = video->RAWC.binning_y + video->RAWC.skipping_y;

        if( sampling_x == 0 ) return 0;
        return ( (float)sampling_y / (float)sampling_x );
    }
    return 0;
}

void printMlvInfo(mlvObject_t * video)
{
    printf("\nMLV Info\n\n");
    printf("      MLV Version: %s\n", video->MLVI.versionString);
    printf("      File Blocks: %lu\n", video->block_num);
    printf("\nLens Info\n\n");
    printf("       Lens Model: %s\n", video->LENS.lensName);
    printf("    Serial Number: %s\n", video->LENS.lensSerial);
    printf("\nCamera Info\n\n");
    printf("     Camera Model: %s\n", video->IDNT.cameraName);
    printf("    Serial Number: %s\n", video->IDNT.cameraSerial);
    printf("\nVideo Info\n\n");
    printf("     X Resolution: %i\n", video->RAWI.xRes);
    printf("     Y Resolution: %i\n", video->RAWI.yRes);
    printf("     Total Frames: %i\n", video->frames);
    printf("       Frame Rate: %.3f\n", video->frame_rate);
    printf("\nExposure Info\n\n");
    printf("          Shutter: 1/%.1f\n", (float)1000000 / (float)video->EXPO.shutterValue);
    printf("      ISO Setting: %i\n", video->EXPO.isoValue);
    printf("     Digital Gain: %i\n", video->EXPO.digitalGain);
    printf("\nRAW Info\n\n");
    printf("      Black Level: %i\n", video->RAWI.raw_info.black_level);
    printf("      White Level: %i\n", video->RAWI.raw_info.white_level);
    printf("     Bits / Pixel: %i\n\n", video->RAWI.raw_info.bits_per_pixel);
}

/* WB Picker function; selects the pic and sends task to processing module */
void findMlvWhiteBalance(mlvObject_t *video, uint64_t frameIndex, int posX, int posY, int *wbTemp, int *wbTint, int mode)
{
    /* Useful */
    int width = getMlvWidth(video);
    int height = getMlvHeight(video);

    /* Size of RAW frame */
    int rgb_frame_size = height * width * 3;

    /* Unprocessed debayered frame (RGB) */
    uint16_t * unprocessed_frame = malloc( rgb_frame_size * sizeof(uint16_t) );

    /* Get the raw data in B&W */
    getMlvRawFrameDebayered(video, frameIndex, unprocessed_frame);

    /* find WB.......... */
    processingFindWhiteBalance( video->processing,
                                width, height,
                                unprocessed_frame,
                                posX, posY,
                                wbTemp, wbTint, mode);

    free(unprocessed_frame);
}

void findMlvWhiteBalanceIsolated(mlvObject_t *video, uint64_t frameIndex, int posX, int posY, int *wbTemp, int *wbTint, int mode)
{
    int width = getMlvWidth(video);
    int height = getMlvHeight(video);
    size_t pixels = (size_t)width * (size_t)height;
    size_t rgb_words = pixels * 3u;

    float * temp_frame = malloc(pixels * sizeof(float));
    uint16_t * unprocessed_frame = malloc(rgb_words * sizeof(uint16_t));
    processingObject_t * analysis_processing = processingCloneForAnalysis(video->processing);

    if (!temp_frame || !unprocessed_frame || !analysis_processing)
    {
        free(temp_frame);
        free(unprocessed_frame);
        processingFreeClone(analysis_processing);
        findMlvWhiteBalance(video, frameIndex, posX, posY, wbTemp, wbTint, mode);
        return;
    }

    get_mlv_raw_frame_debayered_isolated_analysis(video,
                                                  frameIndex,
                                                  temp_frame,
                                                  unprocessed_frame,
                                                  doesMlvAlwaysUseAmaze(video));

    processingFindWhiteBalance(analysis_processing,
                               width, height,
                               unprocessed_frame,
                               posX, posY,
                               wbTemp, wbTint, mode);

    processingFreeClone(analysis_processing);
    free(temp_frame);
    free(unprocessed_frame);
}
