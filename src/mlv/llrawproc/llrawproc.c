/*
 * Copyright (C) 2017 Bouncyball
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

#include <stdio.h>
#include <stdint.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#ifdef _OPENMP
#include <omp.h>
#endif

#include "llrawproc.h"
#include "pixelproc.h"
#include "stripes.h"
#include "patternnoise.h"
#include "dualiso.h"
#include "hist.h"
#include "darkframe.h"
#include "../../../tools/gpu/igpu_recon.h"
#include "../../debug/StageTiming.h"
#include "../../processing/raw_processing.h"
#include "../pipeline_stage_capture.h"
#include "../video_mlv.h"

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

#define MIN(a,b) (((a)<(b))?(a):(b))
#define MAX(a,b) (((a)>(b))?(a):(b))
#define COERCE(x,lo,hi) MAX(MIN((x),(hi)),(lo))
#define ABS(a) ((a) > 0 ? (a) : -(a))

#if defined(_MSC_VER)
#define MLV_THREAD_LOCAL __declspec(thread)
#else
#define MLV_THREAD_LOCAL __thread
#endif

static MLV_THREAD_LOCAL double g_llrawproc_last_shared_lock_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_dualiso_refine_lock_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_publish_lock_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_total_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_dark_frame_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_vertical_stripes_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_focus_pixels_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_bad_pixels_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_pattern_noise_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_pre_dualiso_fix_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_dual_iso_ms = 0.0;
static MLV_THREAD_LOCAL double g_llrawproc_last_chroma_smooth_ms = 0.0;
static MLV_THREAD_LOCAL dualiso_full20bit_timing_t g_llrawproc_last_dual_iso_full20bit_timing = {0};
static double g_llrawproc_last_preview_histogram_ms = 0.0;
static double g_llrawproc_last_preview_regression_ms = 0.0;
static double g_llrawproc_last_preview_rowscale_ms = 0.0;
static MLV_THREAD_LOCAL uint64_t g_llrawproc_debug_pixel_map_copy_count = 0;
static MLV_THREAD_LOCAL uint64_t g_llrawproc_debug_dark_frame_copy_count = 0;
static MLV_THREAD_LOCAL uint64_t g_llrawproc_debug_runtime_publish_count = 0;
static MLV_THREAD_LOCAL int g_llrawproc_analysis_isolation_enabled = 0;
static MLV_THREAD_LOCAL int g_llrawproc_analysis_chroma_smooth_override_enabled = 0;
static MLV_THREAD_LOCAL int g_llrawproc_analysis_chroma_smooth_override = CS_OFF;

static int llrawproc_analysis_thread_count(mlvObject_t * video, int isolated_analysis)
{
    if (isolated_analysis)
    {
        return 1;
    }
    return video ? video->cpu_cores : 1;
}

static int llrawproc_normalize_chroma_smooth_method(int method)
{
    if (method < CS_OFF || method > CS_5x5)
    {
        return CS_OFF;
    }
    return method;
}

static int llrawproc_analysis_chroma_smooth_method(int shared_method,
                                                   int isolated_analysis)
{
    if (isolated_analysis
     && g_llrawproc_analysis_chroma_smooth_override_enabled)
    {
        return g_llrawproc_analysis_chroma_smooth_override;
    }
    return shared_method;
}

/* Diagnostic-only escape hatch: set MLVAPP_DISABLE_DUALISO_PLAYBACK_MEAN23_OVERRIDE=1
 * to make applyLLRawProcObject ignore the diso_playback_force_mean23 field
 * and let the receipt's diso_averaging flow through untouched. This is the
 * peer of MLVAPP_PROFILE_DISABLE_DUALISO_OVERRIDE (which exists on the GUI
 * side at platform/qt/DualIsoPlaybackPolicy.h:24-43); both are intended for
 * A/B harness runs where we want to measure the cost of the override
 * itself. NOT for production playback.
 *
 * Cached after first read so the per-frame fast-path stays branchless;
 * tests reset it via llrpReinitMean23OverrideDispatchForTesting() (mirrors
 * dualisoHqReinitDispatchForTesting at dualiso.c). */
static int g_dualiso_playback_mean23_override_env_cache = -1;

static void llrawproc_reset_dual_iso_full20bit_timing(void)
{
    memset(&g_llrawproc_last_dual_iso_full20bit_timing,
           0,
           sizeof(g_llrawproc_last_dual_iso_full20bit_timing));
    g_llrawproc_last_dual_iso_full20bit_timing.interp_method = -1;
    g_llrawproc_last_dual_iso_full20bit_timing.final_blend_probe_mode = -1;
    g_llrawproc_last_dual_iso_full20bit_timing.path_kind =
        DUALISO_FULL20_PATH_NONE;
    g_llrawproc_last_dual_iso_full20bit_timing.pattern_source =
        DUALISO_FULL20_PATTERN_SOURCE_NONE;
}

static int dualiso_playback_mean23_override_disabled_via_env(void)
{
    if (g_dualiso_playback_mean23_override_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISABLE_DUALISO_PLAYBACK_MEAN23_OVERRIDE");
        if (v && *v && strcmp(v, "0") != 0
                  && strcmp(v, "false") != 0
                  && strcmp(v, "FALSE") != 0
                  && strcmp(v, "False") != 0)
        {
            g_dualiso_playback_mean23_override_env_cache = 1;
        }
        else
        {
            g_dualiso_playback_mean23_override_env_cache = 0;
        }
    }
    return g_dualiso_playback_mean23_override_env_cache;
}

/* Test-only hook: force re-evaluation of the env-disable cache from the
 * current process env. Mirrors dualisoHqReinitDispatchForTesting. Not in
 * the public header; tests forward-declare it. */
int llrpReinitMean23OverrideDispatchForTesting(void);
int llrpReinitMean23OverrideDispatchForTesting(void)
{
    g_dualiso_playback_mean23_override_env_cache = -1;
    return dualiso_playback_mean23_override_disabled_via_env();
}

/* Phase E5: peer of MLVAPP_DISABLE_DUALISO_PLAYBACK_MEAN23_OVERRIDE for the
 * scale-aware alias_map downgrade. The downgrade is opt-in (default OFF)
 * at the GUI policy layer, so this env-disable is mostly a symmetry tool
 * — set MLVAPP_DISABLE_ALIAS_MAP_DOWNGRADE_OVERRIDE=1 to make the
 * per-frame fast-path treat diso_playback_force_disable_alias_map as if it
 * were 0, even when the GUI policy has flipped it on (e.g. via the
 * MLVAPP_PLAYBACK_DOWNGRADE_ALIAS_MAP_AT_SCALE=1 opt-in). Useful for
 * headless A/B harnesses that want to force the field on for cache-key
 * tests but still measure the full receipt-authored pipeline cost.
 *
 * Cache reset hook for tests: llrpReinitKeepHeavyStagesAtScaleDispatchForTesting. */
static int g_dualiso_playback_disable_alias_map_downgrade_env_cache = -1;

static int dualiso_playback_alias_map_downgrade_disabled_via_env(void)
{
    if (g_dualiso_playback_disable_alias_map_downgrade_env_cache < 0)
    {
        const char * v = getenv("MLVAPP_DISABLE_ALIAS_MAP_DOWNGRADE_OVERRIDE");
        if (v && *v && strcmp(v, "0") != 0
                  && strcmp(v, "false") != 0
                  && strcmp(v, "FALSE") != 0
                  && strcmp(v, "False") != 0)
        {
            g_dualiso_playback_disable_alias_map_downgrade_env_cache = 1;
        }
        else
        {
            g_dualiso_playback_disable_alias_map_downgrade_env_cache = 0;
        }
    }
    return g_dualiso_playback_disable_alias_map_downgrade_env_cache;
}

int llrpReinitKeepHeavyStagesAtScaleDispatchForTesting(void);
int llrpReinitKeepHeavyStagesAtScaleDispatchForTesting(void)
{
    g_dualiso_playback_disable_alias_map_downgrade_env_cache = -1;
    return dualiso_playback_alias_map_downgrade_disabled_via_env();
}

static int llrawproc_env_truthy_value(const char * v)
{
    return v && *v
        && strcmp(v, "0") != 0
        && strcmp(v, "false") != 0
        && strcmp(v, "FALSE") != 0
        && strcmp(v, "False") != 0;
}

static int llrawproc_gpu_export_enabled(void)
{
    return llrawproc_env_truthy_value(getenv("MLVAPP_GPU_EXPORT"));
}

static int llrawproc_gpu_export_trusted_enabled(void)
{
    return llrawproc_env_truthy_value(getenv("MLVAPP_GPU_EXPORT_TRUSTED"));
}

static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_recon_allowed = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_texture_present_preferred = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_texture_prepare_only_allowed = 0;

void llrpSetGpuPlaybackReconAllowedForCurrentThread(int enabled);
void llrpSetGpuPlaybackReconAllowedForCurrentThread(int enabled)
{
    g_llrawproc_gpu_playback_recon_allowed = enabled != 0;
}

void llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(int enabled);
void llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(int enabled)
{
    g_llrawproc_gpu_playback_texture_present_preferred = enabled != 0;
}

void llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(int enabled);
void llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(int enabled)
{
    g_llrawproc_gpu_playback_texture_prepare_only_allowed = enabled != 0;
}

static int llrawproc_gpu_playback_recon_enabled(void)
{
    return g_llrawproc_gpu_playback_recon_allowed
        && llrawproc_env_truthy_value(getenv("MLVAPP_GPU_PLAYBACK_RECON"));
}

static int llrawproc_gpu_playback_retain_device_output_enabled(void)
{
    return llrawproc_env_truthy_value(
        getenv("MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT"));
}

static int llrawproc_gpu_playback_recon_output_validation_enabled(void)
{
    return llrawproc_env_truthy_value(
        getenv("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT"));
}

static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_run_attempted = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_run_rc = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_replaced = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_trusted = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_mismatch = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_apply_dither = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_allocated_bytes_valid = 0;
static MLV_THREAD_LOCAL uint64_t g_llrawproc_gpu_export_last_allocated_bytes = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_skip_code = LLRP_GPU_EXPORT_SKIP_NONE;
static MLV_THREAD_LOCAL unsigned long long g_llrawproc_gpu_export_last_mismatch_count = 0;
static MLV_THREAD_LOCAL unsigned long long g_llrawproc_gpu_export_last_mismatch_first_index = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_mismatch_first_cpu = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_mismatch_first_gpu = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_export_last_mismatch_max_abs = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_last_run_attempted = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_last_run_rc = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_last_used = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_last_state_valid = 0;
static MLV_THREAD_LOCAL int g_llrawproc_gpu_playback_last_prepare_only = 0;
static MLV_THREAD_LOCAL dualiso_gpu_recon_state_t g_llrawproc_gpu_playback_last_prepared_state = {0};
static MLV_THREAD_LOCAL uint16_t *g_llrawproc_gpu_playback_last_input_bayer16 = NULL;
static MLV_THREAD_LOCAL size_t g_llrawproc_gpu_playback_last_input_words = 0;
static MLV_THREAD_LOCAL llrpGpuPlaybackRetainedDeviceBayer16_t
    g_llrawproc_gpu_playback_last_retained_device_bayer16 = {0};

static void llrawproc_gpu_export_reset_last_run_state(void)
{
    g_llrawproc_gpu_export_last_run_attempted = 0;
    g_llrawproc_gpu_export_last_run_rc = 0;
    g_llrawproc_gpu_export_last_replaced = 0;
    g_llrawproc_gpu_export_last_trusted = 0;
    g_llrawproc_gpu_export_last_mismatch = 0;
    g_llrawproc_gpu_export_last_apply_dither = 0;
    g_llrawproc_gpu_export_last_allocated_bytes_valid = 0;
    g_llrawproc_gpu_export_last_allocated_bytes = 0;
    g_llrawproc_gpu_export_last_skip_code = LLRP_GPU_EXPORT_SKIP_NONE;
    g_llrawproc_gpu_export_last_mismatch_count = 0;
    g_llrawproc_gpu_export_last_mismatch_first_index = 0;
    g_llrawproc_gpu_export_last_mismatch_first_cpu = 0;
    g_llrawproc_gpu_export_last_mismatch_first_gpu = 0;
    g_llrawproc_gpu_export_last_mismatch_max_abs = 0;
}

static void llrawproc_gpu_export_set_skip_code(int skip_code)
{
    if(!g_llrawproc_gpu_export_last_run_attempted)
    {
        g_llrawproc_gpu_export_last_skip_code = skip_code;
    }
}

static void llrawproc_gpu_playback_reset_last_run_state(void)
{
    g_llrawproc_gpu_playback_last_run_attempted = 0;
    g_llrawproc_gpu_playback_last_run_rc = 0;
    g_llrawproc_gpu_playback_last_used = 0;
    g_llrawproc_gpu_playback_last_state_valid = 0;
    g_llrawproc_gpu_playback_last_prepare_only = 0;
    memset(&g_llrawproc_gpu_playback_last_prepared_state, 0,
           sizeof(g_llrawproc_gpu_playback_last_prepared_state));
    if(g_llrawproc_gpu_playback_last_input_bayer16)
    {
        free(g_llrawproc_gpu_playback_last_input_bayer16);
        g_llrawproc_gpu_playback_last_input_bayer16 = NULL;
    }
    g_llrawproc_gpu_playback_last_input_words = 0;
    memset(&g_llrawproc_gpu_playback_last_retained_device_bayer16,
           0,
           sizeof(g_llrawproc_gpu_playback_last_retained_device_bayer16));
}

static int llrawproc_gpu_playback_take_last_input_bayer16(uint16_t * owned_input,
                                                          size_t input_words)
{
    if(!owned_input || input_words == 0)
    {
        if(g_llrawproc_gpu_playback_last_input_bayer16)
        {
            free(g_llrawproc_gpu_playback_last_input_bayer16);
            g_llrawproc_gpu_playback_last_input_bayer16 = NULL;
        }
        g_llrawproc_gpu_playback_last_input_words = 0;
        return 0;
    }

    if(g_llrawproc_gpu_playback_last_input_bayer16)
    {
        free(g_llrawproc_gpu_playback_last_input_bayer16);
    }
    g_llrawproc_gpu_playback_last_input_bayer16 = owned_input;
    g_llrawproc_gpu_playback_last_input_words = input_words;
    return 1;
}

int llrpResetGpuExportRunForTesting(void);
int llrpResetGpuExportRunForTesting(void)
{
    llrawproc_gpu_export_reset_last_run_state();
    return 1;
}

int llrpResetGpuPlaybackReconRunForTesting(void);
int llrpResetGpuPlaybackReconRunForTesting(void)
{
    llrawproc_gpu_playback_reset_last_run_state();
    return 1;
}

int llrpGpuExportLastRunAttemptedForTesting(void);
int llrpGpuExportLastRunAttemptedForTesting(void)
{
    return g_llrawproc_gpu_export_last_run_attempted;
}

int llrpGpuExportLastRunRcForTesting(void);
int llrpGpuExportLastRunRcForTesting(void)
{
    return g_llrawproc_gpu_export_last_run_rc;
}

int llrpGpuExportLastReplacedForTesting(void);
int llrpGpuExportLastReplacedForTesting(void)
{
    return g_llrawproc_gpu_export_last_replaced;
}

int llrpGpuExportLastTrustedForTesting(void);
int llrpGpuExportLastTrustedForTesting(void)
{
    return g_llrawproc_gpu_export_last_trusted;
}

int llrpGpuExportLastMismatchForTesting(void);
int llrpGpuExportLastMismatchForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch;
}

int llrpGpuExportLastApplyDitherForTesting(void);
int llrpGpuExportLastApplyDitherForTesting(void)
{
    return g_llrawproc_gpu_export_last_apply_dither;
}

unsigned long long llrpGpuExportLastMismatchCountForTesting(void);
unsigned long long llrpGpuExportLastMismatchCountForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch_count;
}

unsigned long long llrpGpuExportLastMismatchFirstIndexForTesting(void);
unsigned long long llrpGpuExportLastMismatchFirstIndexForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch_first_index;
}

int llrpGpuExportLastMismatchFirstCpuForTesting(void);
int llrpGpuExportLastMismatchFirstCpuForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch_first_cpu;
}

int llrpGpuExportLastMismatchFirstGpuForTesting(void);
int llrpGpuExportLastMismatchFirstGpuForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch_first_gpu;
}

int llrpGpuExportLastMismatchMaxAbsForTesting(void);
int llrpGpuExportLastMismatchMaxAbsForTesting(void)
{
    return g_llrawproc_gpu_export_last_mismatch_max_abs;
}

int llrpGpuExportLastSkipCodeForTesting(void);
int llrpGpuExportLastSkipCodeForTesting(void)
{
    return g_llrawproc_gpu_export_last_skip_code;
}

int llrpGpuPlaybackReconLastRunAttemptedForTesting(void);
int llrpGpuPlaybackReconLastRunAttemptedForTesting(void)
{
    return g_llrawproc_gpu_playback_last_run_attempted;
}

int llrpGpuPlaybackReconLastRunRcForTesting(void);
int llrpGpuPlaybackReconLastRunRcForTesting(void)
{
    return g_llrawproc_gpu_playback_last_run_rc;
}

int llrpGpuPlaybackReconLastUsedForTesting(void);
int llrpGpuPlaybackReconLastUsedForTesting(void)
{
    return g_llrawproc_gpu_playback_last_used;
}

int llrpGpuPlaybackReconLastStateValidForTesting(void);
int llrpGpuPlaybackReconLastStateValidForTesting(void)
{
    return g_llrawproc_gpu_playback_last_state_valid;
}

int llrpGpuPlaybackReconLastPrepareOnlyForTesting(void);
int llrpGpuPlaybackReconLastPrepareOnlyForTesting(void)
{
    return g_llrawproc_gpu_playback_last_prepare_only;
}

static void llrawproc_gpu_playback_public_state_from_dualiso(
    const dualiso_gpu_recon_state_t * state,
    llrpGpuPlaybackReconState_t * out)
{
    if(!out) return;
    memset(out, 0, sizeof(*out));
    if(!state || !state->valid) return;

    out->valid = state->valid;
    out->width = state->width;
    out->height = state->height;
    out->black_level = state->black_level;
    out->white_level = state->white_level;
    out->white_darkened = state->white_darkened;
    out->black_delta = state->black_delta;
    out->ev_correction = state->ev_correction;
    out->dark_noise = state->dark_noise;
    out->interp_method = state->interp_method;
    out->use_alias_map = state->use_alias_map;
    out->use_fullres = state->use_fullres;
    out->chroma_smooth_method = state->chroma_smooth_method;
    out->playback_preview_scale_factor = state->playback_preview_scale_factor;
    memcpy(out->is_bright, state->is_bright, sizeof(out->is_bright));
    out->raw2ev = state->raw2ev;
    out->ev2raw = state->ev2raw;
    out->mix_curve = state->mix_curve;
    out->fullres_curve = state->fullres_curve;
    out->randn05 = state->randn05;
    out->apply_dither = state->apply_dither;
}

static int llrawproc_gpu_playback_dualiso_state_from_public(
    const llrpGpuPlaybackReconState_t * state,
    dualiso_gpu_recon_state_t * out)
{
    if(!out) return 0;
    memset(out, 0, sizeof(*out));
    if(!state || !state->valid) return 0;
    if(state->width <= 0 || state->height <= 0) return 0;
    if(!state->raw2ev || !state->ev2raw || !state->mix_curve || !state->fullres_curve) return 0;
    if(state->apply_dither && !state->randn05) return 0;

    out->valid = state->valid;
    out->width = state->width;
    out->height = state->height;
    out->black_level = state->black_level;
    out->white_level = state->white_level;
    out->white_darkened = state->white_darkened;
    out->black_delta = state->black_delta;
    out->ev_correction = state->ev_correction;
    out->dark_noise = state->dark_noise;
    out->interp_method = state->interp_method;
    out->use_alias_map = state->use_alias_map;
    out->use_fullres = state->use_fullres;
    out->chroma_smooth_method = state->chroma_smooth_method;
    out->playback_preview_scale_factor = state->playback_preview_scale_factor;
    memcpy(out->is_bright, state->is_bright, sizeof(out->is_bright));
    out->raw2ev = state->raw2ev;
    out->ev2raw = state->ev2raw;
    out->mix_curve = state->mix_curve;
    out->fullres_curve = state->fullres_curve;
    out->randn05 = state->randn05;
    out->apply_dither = state->apply_dither;
    return 1;
}

static int llrawproc_gpu_playback_recon_state_matches_validated_config(
    const dualiso_gpu_recon_state_t * state)
{
    if(!state || !state->valid) return 0;

    /* The no-readback playback path is admitted ONLY for the proven HQ
     * (high-quality) recon config class:
     *   interp_method == 1 (AMaZE), use_alias_map == 0 or 1, use_fullres == 1,
     *   chroma_smooth_method == 0 or 1 (off or 2x2).
     * Any other config (mean/non-AMaZE interp, invalid alias flag, fullres off, or
     * unsupported chroma smoothing) is unproven for the no-readback bridge and
     * must stay fail-closed so the CPU readback path is used instead. */
    if(state->interp_method != 1) return 0;
    if(state->use_alias_map != 0 && state->use_alias_map != 1) return 0;
    if(state->use_fullres != 1) return 0;
    if(state->chroma_smooth_method < 0 || state->chroma_smooth_method > 1) return 0;

    /* Within the HQ-config class, admit by DEFAULT for arbitrary
     * is_bright / ev_correction / black_delta. These vary per clip
     * (auto-correction, exposure matching) and were proven 0-LSB vs the CPU
     * oracle for the live M16-1327 non-base state: cuda_recon_parity passes
     * 0 LSB at every stage for the exact live levels/ev, and the 4090
     * end-to-end run showed the no-readback GL texture == the recon-output
     * bayer oracle 0 LSB (gl_oracle_match=1, mismatch 0). The earlier
     * base-config-only restriction (black_delta==0, |ev|==3.0,
     * is_bright=={1,1,0,0}) was a validation-conservatism gate, not a
     * correctness boundary.
     *
     * NOTE: the legacy diagnostic env MLVAPP_GPU_PLAYBACK_RECON_ALLOW_ANY_HQ_STATE
     * is now a no-op superset -- the HQ class it used to gate is admitted by
     * default, so reaching this point already implies admission. */
    return 1;
}

int llrpGpuPlaybackReconGetLastPreparedState(llrpGpuPlaybackReconState_t * state);
int llrpGpuPlaybackReconGetLastPreparedState(llrpGpuPlaybackReconState_t * state)
{
    llrawproc_gpu_playback_public_state_from_dualiso(
        &g_llrawproc_gpu_playback_last_prepared_state,
        state);
    return state && state->valid;
}

size_t llrpGpuPlaybackReconGetLastInputBayer16(uint16_t * output,
                                               size_t output_words);
size_t llrpGpuPlaybackReconGetLastInputBayer16(uint16_t * output,
                                               size_t output_words)
{
    const size_t words = g_llrawproc_gpu_playback_last_input_words;
    if(!output || output_words == 0 || !g_llrawproc_gpu_playback_last_input_bayer16)
    {
        return words;
    }
    if(output_words < words)
    {
        return words;
    }
    memcpy(output,
           g_llrawproc_gpu_playback_last_input_bayer16,
           words * sizeof(uint16_t));
    return words;
}

int llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out);
int llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out)
{
    if(!retained_out) return 0;
    *retained_out = g_llrawproc_gpu_playback_last_retained_device_bayer16;
    return retained_out->valid
        && retained_out->device_bayer16
        && retained_out->width > 0
        && retained_out->height > 0
        && retained_out->token != 0;
}

void llrpGetLastGpuExportTelemetry(llrpGpuExportTelemetry_t * telemetry)
{
    if(!telemetry) return;
    memset(telemetry, 0, sizeof(*telemetry));
    telemetry->attempted = g_llrawproc_gpu_export_last_run_attempted;
    telemetry->rc = g_llrawproc_gpu_export_last_run_rc;
    telemetry->replaced = g_llrawproc_gpu_export_last_replaced;
    telemetry->trusted = g_llrawproc_gpu_export_last_trusted;
    telemetry->allocated_bytes_valid =
        g_llrawproc_gpu_export_last_allocated_bytes_valid;
    telemetry->allocated_bytes = g_llrawproc_gpu_export_last_allocated_bytes;
    telemetry->skip_code = g_llrawproc_gpu_export_last_skip_code;
}

#if defined(_WIN32)
typedef igpu_recon_backend* (*llrawproc_gpu_create_fn)(const char*);
typedef void (*llrawproc_gpu_destroy_fn)(igpu_recon_backend*);
typedef int (*llrawproc_gpu_abi_version_fn)(igpu_recon_backend*);
typedef const char* (*llrawproc_gpu_describe_fn)(igpu_recon_backend*);
typedef int (*llrawproc_gpu_set_clip_fn)(igpu_recon_backend*, const igpu_recon_clip_t*);
typedef int (*llrawproc_gpu_set_luts_fn)(igpu_recon_backend*, const igpu_recon_luts_t*);
typedef int (*llrawproc_gpu_run_fn)(igpu_recon_backend*,
                                    const igpu_recon_frame_t*,
                                    const uint16_t*,
                                    igpu_recon_out_kind,
                                    uint16_t*,
                                    unsigned int);
typedef int (*llrawproc_gpu_preupload_frame_fn)(igpu_recon_backend*,
                                                uint64_t,
                                                const uint16_t*,
                                                size_t,
                                                int);
typedef int (*llrawproc_gpu_run_preuploaded_fn)(igpu_recon_backend*,
                                                uint64_t,
                                                const igpu_recon_frame_t*,
                                                const uint16_t*,
                                                igpu_recon_out_kind,
                                                uint16_t*,
                                                unsigned int);
typedef int (*llrawproc_gpu_last_preupload_status_fn)(
    igpu_recon_backend*,
    igpu_recon_preupload_status_t*);
typedef int (*llrawproc_gpu_last_timing_fn)(igpu_recon_backend*, igpu_recon_timing_t*);
typedef int (*llrawproc_gpu_last_device_output_fn)(igpu_recon_backend*,
                                                   const uint16_t**,
                                                   int*,
                                                   int*);
typedef int (*llrawproc_gpu_retain_last_device_output_fn)(igpu_recon_backend*,
                                                          const uint16_t**,
                                                          int*,
                                                          int*,
                                                          uint64_t*);
typedef int (*llrawproc_gpu_release_retained_device_output_fn)(igpu_recon_backend*,
                                                               uint64_t);
typedef int (*llrawproc_gpu_copy_last_device_output_to_gl_texture_fn)(
    igpu_recon_backend*,
    unsigned int);
typedef int (*llrawproc_gpu_allocated_bytes_fn)(igpu_recon_backend*, uint64_t*);
typedef int (*llrawproc_gpu_reset_gl_texture_resources_fn)(igpu_recon_backend*);

typedef struct
{
    const int * raw2ev;
    const int * ev2raw;
    const double * mix_curve;
    const double * fullres_curve;
    const float * randn05;
    int black_level;
    int white_level;
    int apply_dither;
    double ev_correction;
    double dark_noise;
} llrawprocGpuReconLutsKey_t;

typedef struct
{
    HMODULE dll;
    int attempted;
    int unavailable;
    char backend_name[128];
    char dll_path[1024];
    char dll_resolved_path[1024];
    igpu_recon_backend * backend;
    llrawproc_gpu_create_fn create;
    llrawproc_gpu_destroy_fn destroy;
    llrawproc_gpu_abi_version_fn abi_version;
    llrawproc_gpu_describe_fn describe;
    llrawproc_gpu_set_clip_fn set_clip;
    llrawproc_gpu_set_luts_fn set_luts;
    llrawproc_gpu_run_fn run;
    llrawproc_gpu_preupload_frame_fn preupload_frame;
    llrawproc_gpu_run_preuploaded_fn run_preuploaded;
    llrawproc_gpu_last_preupload_status_fn last_preupload_status;
    llrawproc_gpu_last_timing_fn last_timing;
    llrawproc_gpu_last_device_output_fn last_device_output;
    llrawproc_gpu_retain_last_device_output_fn retain_last_device_output;
    llrawproc_gpu_release_retained_device_output_fn release_retained_device_output;
    llrawproc_gpu_copy_last_device_output_to_gl_texture_fn copy_last_device_output_to_gl_texture;
    llrawproc_gpu_allocated_bytes_fn allocated_bytes;
    llrawproc_gpu_reset_gl_texture_resources_fn reset_gl_texture_resources;
    int clip_configured;
    igpu_recon_clip_t configured_clip;
    int luts_configured;
    llrawprocGpuReconLutsKey_t configured_luts_key;
} llrawprocGpuExportBackend_t;

static llrawprocGpuExportBackend_t g_llrawproc_gpu_export_backend = {0};
static pthread_mutex_t g_llrawproc_gpu_recon_backend_mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile LONG g_llrawproc_gpu_recon_run_active = 0;
static volatile LONG g_llrawproc_gpu_preupload_bypass_active = 0;

int llrpGpuPlaybackReconGetBackendInfo(llrpGpuPlaybackReconBackendInfo_t * info);
int llrpGpuPlaybackReconGetBackendInfo(llrpGpuPlaybackReconBackendInfo_t * info)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    const char * description = NULL;
    if(!info) return 0;
    memset(info, 0, sizeof(*info));

    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    info->available = (g->backend && !g->unavailable) ? 1 : 0;
    info->attempted = g->attempted;
    info->unavailable = g->unavailable;
    snprintf(info->requested_backend,
             sizeof(info->requested_backend),
             "%s",
             g->backend_name);
    snprintf(info->requested_path,
             sizeof(info->requested_path),
             "%s",
             g->dll_path);
    snprintf(info->resolved_path,
             sizeof(info->resolved_path),
             "%s",
             g->dll_resolved_path);
    if(g->backend && g->describe)
    {
        description = g->describe(g->backend);
    }
    snprintf(info->description,
             sizeof(info->description),
             "%s",
             description ? description : "");
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
    return 1;
}

static const char * llrawproc_gpu_recon_backend_dll_path(int prefer_playback_dll)
{
    const char * dll_path = NULL;

    if(prefer_playback_dll)
    {
        dll_path = getenv("MLVAPP_GPU_PLAYBACK_RECON_DLL");
        if(!dll_path || !*dll_path) dll_path = getenv("MLVAPP_GPU_RECON_DLL");
        if(!dll_path || !*dll_path) dll_path = getenv("MLVAPP_GPU_EXPORT_DLL");
    }
    else
    {
        dll_path = getenv("MLVAPP_GPU_EXPORT_DLL");
        if(!dll_path || !*dll_path) dll_path = getenv("MLVAPP_GPU_RECON_DLL");
    }

    return (dll_path && *dll_path) ? dll_path : "igpu_recon_cuda.dll";
}

static const char * llrawproc_gpu_recon_backend_name(int prefer_playback_dll)
{
    const char * backend_name = NULL;

    if(prefer_playback_dll)
    {
        backend_name = getenv("MLVAPP_GPU_PLAYBACK_RECON_BACKEND");
        if(!backend_name || !*backend_name) backend_name = getenv("MLVAPP_GPU_RECON_BACKEND");
        if(!backend_name || !*backend_name) backend_name = getenv("MLVAPP_GPU_EXPORT_BACKEND");
    }
    else
    {
        backend_name = getenv("MLVAPP_GPU_EXPORT_BACKEND");
        if(!backend_name || !*backend_name) backend_name = getenv("MLVAPP_GPU_RECON_BACKEND");
    }

    return (backend_name && *backend_name) ? backend_name : "cuda";
}

static void llrawproc_gpu_export_backend_release(llrawprocGpuExportBackend_t * g)
{
    /* A decoder may be submitting N+1 while the recon call owns the main
     * mutex.  Teardown is serialized by that mutex, then waits for the narrow
     * lock-bypass call to return before freeing the backend. */
    while(InterlockedCompareExchange(
              &g_llrawproc_gpu_preupload_bypass_active, 0, 0) != 0)
    {
        Sleep(0);
    }
    if(g->backend && g->destroy)
    {
        g->destroy(g->backend);
    }
    if(g->dll)
    {
        FreeLibrary(g->dll);
    }
    memset(g, 0, sizeof(*g));
}

static void llrawproc_gpu_export_backend_mark_unavailable(llrawprocGpuExportBackend_t * g,
                                                          const char * dll_path,
                                                          const char * backend_name)
{
    llrawproc_gpu_export_backend_release(g);
    g->attempted = 1;
    g->unavailable = 1;
    snprintf(g->backend_name,
             sizeof(g->backend_name),
             "%s",
             backend_name ? backend_name : "");
    snprintf(g->dll_path, sizeof(g->dll_path), "%s", dll_path ? dll_path : "");
}

/* Test-only hook: clear the sticky LoadLibrary result so a focused test can
 * exercise missing-DLL fallback without poisoning later in-process cases. */
int llrpResetGpuExportBackendForTesting(void);
int llrpResetGpuExportBackendForTesting(void)
{
    llrawproc_gpu_export_backend_release(&g_llrawproc_gpu_export_backend);
    llrawproc_gpu_export_reset_last_run_state();
    return 1;
}

int llrpGpuExportBackendAttemptedForTesting(void);
int llrpGpuExportBackendAttemptedForTesting(void)
{
    return g_llrawproc_gpu_export_backend.attempted;
}

int llrpGpuExportBackendUnavailableForTesting(void);
int llrpGpuExportBackendUnavailableForTesting(void)
{
    return g_llrawproc_gpu_export_backend.unavailable;
}

static FARPROC llrawproc_gpu_export_resolve(HMODULE dll, const char * name)
{
    FARPROC proc = GetProcAddress(dll, name);
    if(!proc)
    {
#ifndef STDOUT_SILENT
        printf("MLVAPP_GPU_EXPORT: missing backend symbol %s\n", name);
#endif
    }
    return proc;
}

static int llrawproc_gpu_export_backend_available(int prefer_playback_dll)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    const char * dll_path = llrawproc_gpu_recon_backend_dll_path(prefer_playback_dll);
    const char * backend_name = llrawproc_gpu_recon_backend_name(prefer_playback_dll);

    if(g->backend)
    {
        if(strcmp(g->dll_path, dll_path) == 0
        && strcmp(g->backend_name, backend_name) == 0) return 1;
        llrawproc_gpu_export_backend_release(g);
    }
    if(g->unavailable)
    {
        if(strcmp(g->dll_path, dll_path) == 0
        && strcmp(g->backend_name, backend_name) == 0) return 0;
        llrawproc_gpu_export_backend_release(g);
    }

    g->attempted = 1;
    snprintf(g->backend_name, sizeof(g->backend_name), "%s", backend_name);
    snprintf(g->dll_path, sizeof(g->dll_path), "%s", dll_path);

    g->dll = LoadLibraryA(dll_path);
    if(!g->dll)
    {
        llrawproc_gpu_export_backend_mark_unavailable(g, dll_path, backend_name);
        return 0;
    }
    if(!GetModuleFileNameA(g->dll, g->dll_resolved_path, sizeof(g->dll_resolved_path)))
    {
        snprintf(g->dll_resolved_path,
                 sizeof(g->dll_resolved_path),
                 "%s",
                 dll_path);
    }

#define LLRAWPROC_GPU_RESOLVE_TYPED(member, type, symbol) \
    do { \
        union { FARPROC raw; type typed; } resolved; \
        resolved.raw = llrawproc_gpu_export_resolve(g->dll, symbol); \
        g->member = resolved.typed; \
    } while(0)

    LLRAWPROC_GPU_RESOLVE_TYPED(create, llrawproc_gpu_create_fn, "igpu_recon_create");
    LLRAWPROC_GPU_RESOLVE_TYPED(destroy, llrawproc_gpu_destroy_fn, "igpu_recon_destroy");
    LLRAWPROC_GPU_RESOLVE_TYPED(abi_version, llrawproc_gpu_abi_version_fn, "igpu_recon_abi_version");
    LLRAWPROC_GPU_RESOLVE_TYPED(describe, llrawproc_gpu_describe_fn, "igpu_recon_describe");
    LLRAWPROC_GPU_RESOLVE_TYPED(set_clip, llrawproc_gpu_set_clip_fn, "igpu_recon_set_clip");
    LLRAWPROC_GPU_RESOLVE_TYPED(set_luts, llrawproc_gpu_set_luts_fn, "igpu_recon_set_luts");
    LLRAWPROC_GPU_RESOLVE_TYPED(run, llrawproc_gpu_run_fn, "igpu_recon_run");
    LLRAWPROC_GPU_RESOLVE_TYPED(last_timing, llrawproc_gpu_last_timing_fn, "igpu_recon_last_timing");
    {
        union { FARPROC raw; llrawproc_gpu_preupload_frame_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_preupload_frame");
        g->preupload_frame = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_run_preuploaded_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_run_preuploaded");
        g->run_preuploaded = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_last_preupload_status_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_last_preupload_status");
        g->last_preupload_status = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_last_device_output_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_last_device_output");
        g->last_device_output = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_retain_last_device_output_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_retain_last_device_output");
        g->retain_last_device_output = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_release_retained_device_output_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_release_retained_device_output");
        g->release_retained_device_output = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_copy_last_device_output_to_gl_texture_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_copy_last_device_output_to_gl_texture");
        g->copy_last_device_output_to_gl_texture = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_allocated_bytes_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_allocated_bytes");
        g->allocated_bytes = resolved.typed;
    }
    {
        union { FARPROC raw; llrawproc_gpu_reset_gl_texture_resources_fn typed; } resolved;
        resolved.raw = GetProcAddress(g->dll, "igpu_recon_reset_gl_texture_resources");
        g->reset_gl_texture_resources = resolved.typed;
    }

#undef LLRAWPROC_GPU_RESOLVE_TYPED

    if(!g->create || !g->destroy || !g->abi_version || !g->describe ||
       !g->set_clip || !g->set_luts || !g->run || !g->last_timing)
    {
        llrawproc_gpu_export_backend_mark_unavailable(g, dll_path, backend_name);
        return 0;
    }

    g->backend = g->create(backend_name);
    if(!g->backend || g->abi_version(g->backend) != IGPU_RECON_ABI_VERSION)
    {
        if(g->backend)
        {
            g->destroy(g->backend);
        }
        g->backend = NULL;
        llrawproc_gpu_export_backend_mark_unavailable(g, dll_path, backend_name);
        return 0;
    }

    return 1;
}

static int llrawproc_gpu_recon_backend_available_guarded(int prefer_playback_dll)
{
    int available = 0;
    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    available = llrawproc_gpu_export_backend_available(prefer_playback_dll);
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
    return available;
}

int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size);
int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    int locked = 0;
    int run_active = 0;
    int bypass_claimed = 0;
    int rc = 0;
    if(!llrawproc_env_truthy_value(
           getenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D"))
     || !raw_input_bayer14
     || raw_image_size == 0)
    {
        return 0;
    }

    locked = pthread_mutex_trylock(&g_llrawproc_gpu_recon_backend_mutex) == 0;
    if(!locked)
    {
        /* Claim before inspecting run_active so teardown, if it owns the
         * mutex, must wait before freeing the backend pointer. */
        InterlockedIncrement(&g_llrawproc_gpu_preupload_bypass_active);
        bypass_claimed = 1;
    }
    run_active =
        InterlockedCompareExchange(&g_llrawproc_gpu_recon_run_active, 0, 0) != 0;
    if(!locked && !run_active)
    {
        InterlockedDecrement(&g_llrawproc_gpu_preupload_bypass_active);
        return 0;
    }

    /* If the mutex is held by the active recon call, the backend/function
     * pointers cannot be replaced until that call clears run_active and
     * releases the mutex.  This narrow bypass is what permits N+1 upload to
     * overlap N's kernels without racing backend teardown. */
    if(g->backend
     && g->clip_configured
     && g->preupload_frame
     && g->run_preuploaded
     && g->last_preupload_status)
    {
        rc = g->preupload_frame(g->backend,
                                frame_id + 1u,
                                raw_input_bayer14,
                                raw_image_size,
                                run_active);
    }
    if(locked) pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
    if(bypass_claimed)
    {
        InterlockedDecrement(&g_llrawproc_gpu_preupload_bypass_active);
    }
    return rc == 0;
}

static void llrawproc_gpu_recon_luts_key_from_state(
    const dualiso_gpu_recon_state_t * state,
    llrawprocGpuReconLutsKey_t * key)
{
    if(!key) return;
    memset(key, 0, sizeof(*key));
    if(!state) return;
    key->raw2ev = state->raw2ev;
    key->ev2raw = state->ev2raw;
    key->mix_curve = state->mix_curve;
    key->fullres_curve = state->fullres_curve;
    key->randn05 = state->randn05;
    key->black_level = state->black_level;
    key->white_level = state->white_level;
    key->apply_dither = state->apply_dither;
    key->ev_correction = state->ev_correction;
    key->dark_noise = state->dark_noise;
}

static int llrawproc_gpu_recon_run_backend(const dualiso_gpu_recon_state_t * state,
                                           const uint16_t * gpu_input,
                                           uint16_t * gpu_output,
                                           unsigned int gl_texture_id,
                                           igpu_recon_out_kind out_kind,
                                           size_t raw_image_size,
                                           int prefer_playback_dll,
                                           uint64_t frame_id,
                                           int * rc_out,
                                           uint64_t * allocated_bytes_out,
                                           int * allocated_bytes_valid_out,
                                           llrpGpuPlaybackReconTiming_t * timing_out,
                                           const uint16_t ** device_bayer16_out,
                                           int * device_width_out,
                                           int * device_height_out,
                                           llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    igpu_recon_clip_t clip;
    igpu_recon_luts_t luts;
    igpu_recon_frame_t frame;
    llrawprocGpuReconLutsKey_t luts_key;
    int rc = 0;
    const size_t pixel_count = raw_image_size / sizeof(uint16_t);
    if(rc_out) *rc_out = -1;
    if(allocated_bytes_out) *allocated_bytes_out = 0;
    if(allocated_bytes_valid_out) *allocated_bytes_valid_out = 0;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    if(device_bayer16_out) *device_bayer16_out = NULL;
    if(device_width_out) *device_width_out = 0;
    if(device_height_out) *device_height_out = 0;
    if(retained_out) memset(retained_out, 0, sizeof(*retained_out));
    if(!state || !state->valid || !gpu_input || raw_image_size == 0) return 0;
    if(out_kind == IGPU_OUT_CPU16 && !gpu_output) return 0;
    if(out_kind == IGPU_OUT_GL_TEXTURE && gl_texture_id == 0) return 0;
    if(pixel_count != (size_t)state->width * (size_t)state->height) return 0;
    if(prefer_playback_dll
     && !llrawproc_gpu_playback_recon_state_matches_validated_config(state))
    {
        if(rc_out) *rc_out = LLRP_GPU_PLAYBACK_RECON_RC_UNSUPPORTED_STATE;
        return 0;
    }

    memset(&clip, 0, sizeof(clip));
    clip.width = state->width;
    clip.height = state->height;
    clip.black_level = state->black_level;
    clip.white_level = state->white_level;
    memcpy(clip.is_bright, state->is_bright, sizeof(clip.is_bright));

    memset(&luts, 0, sizeof(luts));
    luts.raw2ev = state->raw2ev;
    luts.ev2raw = state->ev2raw;
    luts.mix_curve = state->mix_curve;
    luts.fullres_curve = state->fullres_curve;
    luts.randn05 = state->randn05;
    llrawproc_gpu_recon_luts_key_from_state(state, &luts_key);

    memset(&frame, 0, sizeof(frame));
    frame.ev_correction = state->ev_correction;
    frame.black_delta = state->black_delta;
    frame.white_darkened = state->white_darkened;
    frame.dark_noise = state->dark_noise;
    frame.interp_method = state->interp_method;
    frame.use_alias_map = state->use_alias_map;
    frame.use_fullres = state->use_fullres;
    frame.chroma_smooth_method = state->chroma_smooth_method;
    frame.playback_preview_scale_factor = state->playback_preview_scale_factor;
    frame.apply_dither = state->apply_dither;

    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    if(!llrawproc_gpu_export_backend_available(prefer_playback_dll))
    {
        pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
        return 0;
    }
    if(out_kind == IGPU_OUT_DEVICE_BAYER16 && !g->last_device_output)
    {
        pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
        if(rc_out) *rc_out = -3;
        return 0;
    }
    const int clip_matches =
        g->clip_configured
        && memcmp(&g->configured_clip, &clip, sizeof(clip)) == 0;
    const int luts_match =
        g->luts_configured
        && memcmp(&g->configured_luts_key, &luts_key, sizeof(luts_key)) == 0;
    const int need_set_clip = !clip_matches;
    const int need_set_luts = need_set_clip || !luts_match;

    if(need_set_clip)
    {
        rc = g->set_clip(g->backend, &clip);
        if(rc == 0)
        {
            g->configured_clip = clip;
            g->clip_configured = 1;
            g->luts_configured = 0;
        }
        else
        {
            g->clip_configured = 0;
            g->luts_configured = 0;
        }
    }
    if(rc == 0 && need_set_luts)
    {
        rc = g->set_luts(g->backend, &luts);
        if(rc == 0)
        {
            g->configured_luts_key = luts_key;
            g->luts_configured = 1;
        }
        else
        {
            g->luts_configured = 0;
        }
    }
    if(rc == 0)
    {
        InterlockedExchange(&g_llrawproc_gpu_recon_run_active, 1);
        if(prefer_playback_dll
         && llrawproc_env_truthy_value(
                getenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D"))
         && frame_id != 0
         && g->run_preuploaded
         && g->last_preupload_status)
        {
            rc = g->run_preuploaded(g->backend,
                                    frame_id,
                                    &frame,
                                    gpu_input,
                                    out_kind,
                                    gpu_output,
                                    gl_texture_id);
        }
        else
        {
            rc = g->run(g->backend,
                        &frame,
                        gpu_input,
                        out_kind,
                        gpu_output,
                        gl_texture_id);
        }
        InterlockedExchange(&g_llrawproc_gpu_recon_run_active, 0);
        if(prefer_playback_dll && g->last_preupload_status && timing_out)
        {
            igpu_recon_preupload_status_t status;
            memset(&status, 0, sizeof(status));
            if(g->last_preupload_status(g->backend, &status) == 0)
            {
                timing_out->preupload.available = 1;
                timing_out->preupload.accepted = status.accepted;
                timing_out->preupload.used = status.used;
                timing_out->preupload.exact_match = status.exact_match;
                timing_out->preupload.submitted_while_prior_run_active =
                    status.submitted_while_prior_run_active;
                timing_out->preupload.ready_before_run =
                    status.ready_before_run;
                timing_out->preupload.host_staging_ms =
                    status.host_staging_ms;
                timing_out->preupload.upload_ms = status.upload_ms;
                timing_out->preupload.upload_wait_ms = status.upload_wait_ms;
            }
        }
    }
    if(rc == 0
     && out_kind == IGPU_OUT_DEVICE_BAYER16
     && g->last_device_output
     && device_bayer16_out
     && device_width_out
     && device_height_out)
    {
        const uint16_t * device_bayer16 = NULL;
        int device_width = 0;
        int device_height = 0;
        const int device_rc =
            g->last_device_output(g->backend,
                                  &device_bayer16,
                                  &device_width,
                                  &device_height);
        if(device_rc == 0
        && device_bayer16
        && device_width == state->width
        && device_height == state->height)
        {
            *device_bayer16_out = device_bayer16;
            *device_width_out = device_width;
            *device_height_out = device_height;
        }
        else
        {
            rc = device_rc != 0 ? device_rc : -4;
        }
    }
    if(rc == 0
     && out_kind == IGPU_OUT_DEVICE_BAYER16
     && retained_out
     && !g->retain_last_device_output)
    {
        rc = -6;
    }
    if(rc == 0
     && out_kind == IGPU_OUT_DEVICE_BAYER16
     && retained_out
     && g->retain_last_device_output)
    {
        const uint16_t * retained_device_bayer16 = NULL;
        int retained_width = 0;
        int retained_height = 0;
        uint64_t retained_token = 0;
        const int retained_rc =
            g->retain_last_device_output(g->backend,
                                         &retained_device_bayer16,
                                         &retained_width,
                                         &retained_height,
                                         &retained_token);
        if(retained_rc == 0
        && retained_device_bayer16
        && retained_width == state->width
        && retained_height == state->height
        && retained_token != 0)
        {
            retained_out->valid = 1;
            retained_out->device_bayer16 = retained_device_bayer16;
            retained_out->width = retained_width;
            retained_out->height = retained_height;
            retained_out->token = retained_token;
            if(device_bayer16_out) *device_bayer16_out = retained_device_bayer16;
            if(device_width_out) *device_width_out = retained_width;
            if(device_height_out) *device_height_out = retained_height;
        }
        else
        {
            rc = retained_rc != 0 ? retained_rc : -5;
        }
    }
    if(g->allocated_bytes && allocated_bytes_out && allocated_bytes_valid_out)
    {
        uint64_t allocated_bytes = 0;
        if(g->allocated_bytes(g->backend, &allocated_bytes) == 0)
        {
            *allocated_bytes_out = allocated_bytes;
            *allocated_bytes_valid_out = 1;
        }
    }
    if(g->last_timing && timing_out)
    {
        igpu_recon_timing_t timing;
        memset(&timing, 0, sizeof(timing));
        if(g->last_timing(g->backend, &timing) == 0)
        {
            timing_out->available = 1;
            timing_out->upload_ms = timing.upload_ms;
            timing_out->kernel_ms = timing.kernel_ms;
            timing_out->interop_ms = timing.download_ms;
            timing_out->total_ms = timing.total_ms;
        }
    }
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);

    if(rc_out) *rc_out = rc;
    return rc == 0;
}

int llrpGpuPlaybackReconResetGlTextureResources(void);
int llrpGpuPlaybackReconResetGlTextureResources(void)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    int ok = 1;
    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    if(g->backend && g->reset_gl_texture_resources)
    {
        ok = (g->reset_gl_texture_resources(g->backend) == 0) ? 1 : 0;
    }
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);
    return ok;
}

static int llrawproc_gpu_recon_run_cpu16(const dualiso_gpu_recon_state_t * state,
                                         const uint16_t * gpu_input,
                                         uint16_t * gpu_output,
                                         size_t raw_image_size,
                                         int prefer_playback_dll,
                                         uint64_t frame_id,
                                         int * rc_out,
                                         uint64_t * allocated_bytes_out,
                                         int * allocated_bytes_valid_out)
{
    return llrawproc_gpu_recon_run_backend(state,
                                          gpu_input,
                                          gpu_output,
                                          0,
                                          IGPU_OUT_CPU16,
                                          raw_image_size,
                                          prefer_playback_dll,
                                          frame_id,
                                           rc_out,
                                           allocated_bytes_out,
                                           allocated_bytes_valid_out,
                                           NULL,
                                           NULL,
                                           NULL,
                                           NULL,
                                           NULL);
}

static int llrawproc_gpu_export_try_replace(uint16_t * cpu_output,
                                            const uint16_t * gpu_input,
                                            size_t raw_image_size)
{
    dualiso_gpu_recon_state_t state;
    uint16_t * gpu_output = NULL;
    int rc = 0;
    uint64_t allocated_bytes = 0;
    int allocated_bytes_valid = 0;
    const size_t pixel_count = raw_image_size / sizeof(uint16_t);

    llrawproc_gpu_export_reset_last_run_state();
    if(!llrawproc_gpu_export_enabled())
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_DISABLED);
        return 0;
    }
    if(!cpu_output || !gpu_input)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_MISSING_INPUT);
        return 0;
    }
    if(!dualiso_debug_get_last_gpu_recon_state(&state))
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_MISSING_RECON_STATE);
        return 0;
    }
    if(!state.valid)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE);
        return 0;
    }
    if(pixel_count != (size_t)state.width * (size_t)state.height)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_SIZE_MISMATCH);
        return 0;
    }
    g_llrawproc_gpu_export_last_apply_dither = state.apply_dither;

    gpu_output = (uint16_t *)malloc(raw_image_size);
    if(!gpu_output)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_OUTPUT_ALLOC_FAILED);
        return 0;
    }

    g_llrawproc_gpu_export_last_run_attempted = 1;
    g_llrawproc_gpu_export_last_skip_code = LLRP_GPU_EXPORT_SKIP_NONE;
    (void)llrawproc_gpu_recon_run_cpu16(&state,
                                        gpu_input,
                                        gpu_output,
                                        raw_image_size,
                                        0,
                                        0,
                                        &rc,
                                        &allocated_bytes,
                                        &allocated_bytes_valid);
    g_llrawproc_gpu_export_last_run_rc = rc;
    if(allocated_bytes_valid)
    {
        g_llrawproc_gpu_export_last_allocated_bytes_valid = 1;
        g_llrawproc_gpu_export_last_allocated_bytes = allocated_bytes;
    }
    if(rc == 0 && memcmp(cpu_output, gpu_output, raw_image_size) == 0)
    {
        g_llrawproc_gpu_export_last_replaced = 1;
        memcpy(cpu_output, gpu_output, raw_image_size);
        free(gpu_output);
        return 1;
    }
    if(rc == 0)
    {
        unsigned long long mismatch_count = 0;
        for(size_t i = 0; i < pixel_count; ++i)
        {
            int cpu = (int)cpu_output[i];
            int gpu = (int)gpu_output[i];
            int diff = cpu - gpu;
            if(diff)
            {
                int abs_diff = diff < 0 ? -diff : diff;
                if(mismatch_count == 0)
                {
                    g_llrawproc_gpu_export_last_mismatch_first_index = (unsigned long long)i;
                    g_llrawproc_gpu_export_last_mismatch_first_cpu = cpu;
                    g_llrawproc_gpu_export_last_mismatch_first_gpu = gpu;
                }
                if(abs_diff > g_llrawproc_gpu_export_last_mismatch_max_abs)
                {
                    g_llrawproc_gpu_export_last_mismatch_max_abs = abs_diff;
                }
                ++mismatch_count;
            }
        }
        g_llrawproc_gpu_export_last_mismatch_count = mismatch_count;
        g_llrawproc_gpu_export_last_mismatch = 1;
    }

    free(gpu_output);
    return 0;
}

static int llrawproc_gpu_export_try_trusted(const dualiso_gpu_recon_state_t * state,
                                            const uint16_t * gpu_input,
                                            uint16_t * gpu_output,
                                            size_t raw_image_size)
{
    int rc = 0;
    uint64_t allocated_bytes = 0;
    int allocated_bytes_valid = 0;
    const size_t pixel_count = raw_image_size / sizeof(uint16_t);

    llrawproc_gpu_export_reset_last_run_state();
    if(!llrawproc_gpu_export_enabled())
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_DISABLED);
        return 0;
    }
    if(!llrawproc_gpu_export_trusted_enabled())
    {
        return 0;
    }
    if(!gpu_input || !gpu_output)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_MISSING_INPUT);
        return 0;
    }
    if(!state)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_MISSING_RECON_STATE);
        return 0;
    }
    if(!state->valid)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE);
        return 0;
    }
    if(pixel_count != (size_t)state->width * (size_t)state->height)
    {
        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_SIZE_MISMATCH);
        return 0;
    }
    g_llrawproc_gpu_export_last_apply_dither = state->apply_dither;
    g_llrawproc_gpu_export_last_run_attempted = 1;
    g_llrawproc_gpu_export_last_skip_code = LLRP_GPU_EXPORT_SKIP_NONE;
    (void)llrawproc_gpu_recon_run_cpu16(state,
                                        gpu_input,
                                        gpu_output,
                                        raw_image_size,
                                        0,
                                        0,
                                        &rc,
                                        &allocated_bytes,
                                        &allocated_bytes_valid);
    g_llrawproc_gpu_export_last_run_rc = rc;
    if(allocated_bytes_valid)
    {
        g_llrawproc_gpu_export_last_allocated_bytes_valid = 1;
        g_llrawproc_gpu_export_last_allocated_bytes = allocated_bytes;
    }
    if(rc == 0)
    {
        g_llrawproc_gpu_export_last_replaced = 1;
        g_llrawproc_gpu_export_last_trusted = 1;
        return 1;
    }
    return 0;
}

static int llrawproc_gpu_playback_try_reconstruct(const dualiso_gpu_recon_state_t * state,
                                                  const uint16_t * gpu_input,
                                                  uint16_t * gpu_output,
                                                  size_t raw_image_size)
{
    uint16_t * gpu_recon_output = NULL;
    int rc = -1;

    llrawproc_gpu_playback_reset_last_run_state();
    g_llrawproc_gpu_playback_last_state_valid =
        (state && state->valid) ? 1 : 0;
    if(state && state->valid)
    {
        g_llrawproc_gpu_playback_last_prepared_state = *state;
    }
    if(!state || !state->valid || !gpu_input || !gpu_output || raw_image_size == 0)
    {
        return 0;
    }

    gpu_recon_output = (uint16_t *)malloc(raw_image_size);
    if(!gpu_recon_output)
    {
        g_llrawproc_gpu_playback_last_run_rc = -2;
        return 0;
    }

    g_llrawproc_gpu_playback_last_run_attempted = 1;
    /* This call runs synchronously on the SAME thread that set the current
     * frame index just above applyLLRawProcObjectWorker (RenderFrameThread.cpp),
     * so mlv_pipeline_capture_get_current_frame() reliably names the frame
     * this reconstruction is for. Do not replace this with a new ambient
     * thread-local dedicated to GPU recon -- reuse this already-armed,
     * synchronous, same-thread value instead. */
    if(llrawproc_gpu_recon_run_cpu16(state,
                                     gpu_input,
                                     gpu_recon_output,
                                     raw_image_size,
                                     1,
                                     mlv_pipeline_capture_get_current_frame(),
                                     &rc,
                                     NULL,
                                     NULL))
    {
        memcpy(gpu_output, gpu_recon_output, raw_image_size);
        free(gpu_recon_output);
        g_llrawproc_gpu_playback_last_run_rc = rc;
        g_llrawproc_gpu_playback_last_used = 1;
        return 1;
    }

    free(gpu_recon_output);
    g_llrawproc_gpu_playback_last_run_rc = rc;
    return 0;
}

int llrpGpuPlaybackReconRunGlTexture(const llrpGpuPlaybackReconState_t * state,
                                     const uint16_t * raw_input_bayer14,
                                     size_t raw_image_size,
                                     unsigned int gl_texture_id,
                                     int * rc_out,
                                     llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunGlTexture(const llrpGpuPlaybackReconState_t * state,
                                     const uint16_t * raw_input_bayer14,
                                     size_t raw_image_size,
                                     unsigned int gl_texture_id,
                                     int * rc_out,
                                     llrpGpuPlaybackReconTiming_t * timing_out)
{
    dualiso_gpu_recon_state_t private_state;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    if(!llrawproc_gpu_playback_dualiso_state_from_public(state, &private_state))
    {
        return 0;
    }
    return llrawproc_gpu_recon_run_backend(&private_state,
                                          raw_input_bayer14,
                                          NULL,
                                          gl_texture_id,
                                          IGPU_OUT_GL_TEXTURE,
                                          raw_image_size,
                                          1,
                                          state->frame_id,
                                           rc_out,
                                           NULL,
                                           NULL,
                                           timing_out,
                                           NULL,
                                           NULL,
                                           NULL,
                                           NULL);
}

int llrpGpuPlaybackReconRunDeviceBayer16(const llrpGpuPlaybackReconState_t * state,
                                         const uint16_t * raw_input_bayer14,
                                         size_t raw_image_size,
                                         const uint16_t ** device_bayer16_out,
                                         int * width_out,
                                         int * height_out,
                                         int * rc_out,
                                         llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunDeviceBayer16(const llrpGpuPlaybackReconState_t * state,
                                         const uint16_t * raw_input_bayer14,
                                         size_t raw_image_size,
                                         const uint16_t ** device_bayer16_out,
                                         int * width_out,
                                         int * height_out,
                                         int * rc_out,
                                         llrpGpuPlaybackReconTiming_t * timing_out)
{
    dualiso_gpu_recon_state_t private_state;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    if(device_bayer16_out) *device_bayer16_out = NULL;
    if(width_out) *width_out = 0;
    if(height_out) *height_out = 0;
    if(!device_bayer16_out || !width_out || !height_out) return 0;
    if(!llrawproc_gpu_playback_dualiso_state_from_public(state, &private_state))
    {
        return 0;
    }
    return llrawproc_gpu_recon_run_backend(&private_state,
                                          raw_input_bayer14,
                                          NULL,
                                          0,
                                          IGPU_OUT_DEVICE_BAYER16,
                                          raw_image_size,
                                          1,
                                          state->frame_id,
                                          rc_out,
                                          NULL,
                                          NULL,
                                          timing_out,
                                          device_bayer16_out,
                                          width_out,
                                          height_out,
                                          NULL);
}

int llrpGpuPlaybackReconRunRetainedDeviceBayer16(
    const llrpGpuPlaybackReconState_t * state,
    const uint16_t * raw_input_bayer14,
    size_t raw_image_size,
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out,
    int * rc_out,
    llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunRetainedDeviceBayer16(
    const llrpGpuPlaybackReconState_t * state,
    const uint16_t * raw_input_bayer14,
    size_t raw_image_size,
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out,
    int * rc_out,
    llrpGpuPlaybackReconTiming_t * timing_out)
{
    dualiso_gpu_recon_state_t private_state;
    const uint16_t * device_bayer16 = NULL;
    int width = 0;
    int height = 0;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    if(retained_out) memset(retained_out, 0, sizeof(*retained_out));
    if(!retained_out) return 0;
    if(!llrawproc_gpu_playback_dualiso_state_from_public(state, &private_state))
    {
        return 0;
    }
    return llrawproc_gpu_recon_run_backend(&private_state,
                                          raw_input_bayer14,
                                          NULL,
                                          0,
                                          IGPU_OUT_DEVICE_BAYER16,
                                          raw_image_size,
                                          1,
                                          state->frame_id,
                                          rc_out,
                                          NULL,
                                          NULL,
                                          timing_out,
                                          &device_bayer16,
                                          &width,
                                          &height,
                                          retained_out);
}

int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,
                                                         int * rc_out);
int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,
                                                         int * rc_out)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    int rc = -1;
    if(rc_out) *rc_out = -1;
    if(gl_texture_id == 0) return 0;

    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    if(g->backend && !g->unavailable && g->copy_last_device_output_to_gl_texture)
    {
        rc = g->copy_last_device_output_to_gl_texture(g->backend, gl_texture_id);
    }
    else
    {
        rc = -3;
    }
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);

    if(rc_out) *rc_out = rc;
    return rc == 0;
}

int llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(uint64_t token);
int llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(uint64_t token)
{
    llrawprocGpuExportBackend_t * g = &g_llrawproc_gpu_export_backend;
    int rc = -1;
    if(token == 0) return 1;

    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);
    if(g->backend && !g->unavailable && g->release_retained_device_output)
    {
        rc = g->release_retained_device_output(g->backend, token);
    }
    else
    {
        rc = -3;
    }
    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);

    return rc == 0 || rc == 1;
}

int llrpGpuPlaybackReconRunCpu16Probe(const llrpGpuPlaybackReconState_t * state,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size,
                                      uint16_t * output_bayer16,
                                      int * rc_out,
                                      llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunCpu16Probe(const llrpGpuPlaybackReconState_t * state,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size,
                                      uint16_t * output_bayer16,
                                      int * rc_out,
                                      llrpGpuPlaybackReconTiming_t * timing_out)
{
    dualiso_gpu_recon_state_t private_state;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    if(!output_bayer16) return 0;
    if(!llrawproc_gpu_playback_dualiso_state_from_public(state, &private_state))
    {
        return 0;
    }
    return llrawproc_gpu_recon_run_backend(&private_state,
                                          raw_input_bayer14,
                                          output_bayer16,
                                          0,
                                          IGPU_OUT_CPU16,
                                          raw_image_size,
                                          1,
                                          state->frame_id,
                                           rc_out,
                                           NULL,
                                           NULL,
                                           timing_out,
                                           NULL,
                                           NULL,
                                           NULL,
                                           NULL);
}
#else
static int llrawproc_gpu_export_backend_available(int prefer_playback_dll)
{
    (void)prefer_playback_dll;
    return 0;
}

static int llrawproc_gpu_recon_backend_available_guarded(int prefer_playback_dll)
{
    return llrawproc_gpu_export_backend_available(prefer_playback_dll);
}

int llrpResetGpuExportBackendForTesting(void);
int llrpResetGpuExportBackendForTesting(void)
{
    return 1;
}

int llrpGpuExportBackendAttemptedForTesting(void);
int llrpGpuExportBackendAttemptedForTesting(void)
{
    return 0;
}

int llrpGpuExportBackendUnavailableForTesting(void);
int llrpGpuExportBackendUnavailableForTesting(void)
{
    return 1;
}

/* RenderFrameThread is shared by every desktop platform, while the dynamic
 * GPU recon backend is Windows-only.  Keep the public preupload API complete
 * here and fail closed when that backend cannot exist. */
int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size);
int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size)
{
    (void)frame_id;
    (void)raw_input_bayer14;
    (void)raw_image_size;
    return 0;
}

static int llrawproc_gpu_export_try_replace(uint16_t * cpu_output,
                                            const uint16_t * gpu_input,
                                            size_t raw_image_size)
{
    (void)cpu_output;
    (void)gpu_input;
    (void)raw_image_size;
    return 0;
}

static int llrawproc_gpu_export_try_trusted(const dualiso_gpu_recon_state_t * state,
                                            const uint16_t * gpu_input,
                                            uint16_t * gpu_output,
                                            size_t raw_image_size)
{
    (void)state;
    (void)gpu_input;
    (void)gpu_output;
    (void)raw_image_size;
    return 0;
}

static int llrawproc_gpu_playback_try_reconstruct(const dualiso_gpu_recon_state_t * state,
                                                  const uint16_t * gpu_input,
                                                  uint16_t * gpu_output,
                                                  size_t raw_image_size)
{
    (void)state;
    (void)gpu_input;
    (void)gpu_output;
    (void)raw_image_size;
    llrawproc_gpu_playback_reset_last_run_state();
    g_llrawproc_gpu_playback_last_run_attempted = 1;
    g_llrawproc_gpu_playback_last_run_rc = -1;
    return 0;
}

int llrpGpuPlaybackReconRunGlTexture(const llrpGpuPlaybackReconState_t * state,
                                     const uint16_t * raw_input_bayer14,
                                     size_t raw_image_size,
                                     unsigned int gl_texture_id,
                                     int * rc_out,
                                     llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunGlTexture(const llrpGpuPlaybackReconState_t * state,
                                     const uint16_t * raw_input_bayer14,
                                     size_t raw_image_size,
                                     unsigned int gl_texture_id,
                                     int * rc_out,
                                     llrpGpuPlaybackReconTiming_t * timing_out)
{
    (void)state;
    (void)raw_input_bayer14;
    (void)raw_image_size;
    (void)gl_texture_id;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    return 0;
}

int llrpGpuPlaybackReconRunDeviceBayer16(const llrpGpuPlaybackReconState_t * state,
                                         const uint16_t * raw_input_bayer14,
                                         size_t raw_image_size,
                                         const uint16_t ** device_bayer16_out,
                                         int * width_out,
                                         int * height_out,
                                         int * rc_out,
                                         llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunDeviceBayer16(const llrpGpuPlaybackReconState_t * state,
                                         const uint16_t * raw_input_bayer14,
                                         size_t raw_image_size,
                                         const uint16_t ** device_bayer16_out,
                                         int * width_out,
                                         int * height_out,
                                         int * rc_out,
                                         llrpGpuPlaybackReconTiming_t * timing_out)
{
    (void)state;
    (void)raw_input_bayer14;
    (void)raw_image_size;
    if(device_bayer16_out) *device_bayer16_out = NULL;
    if(width_out) *width_out = 0;
    if(height_out) *height_out = 0;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    return 0;
}

int llrpGpuPlaybackReconRunRetainedDeviceBayer16(
    const llrpGpuPlaybackReconState_t * state,
    const uint16_t * raw_input_bayer14,
    size_t raw_image_size,
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out,
    int * rc_out,
    llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunRetainedDeviceBayer16(
    const llrpGpuPlaybackReconState_t * state,
    const uint16_t * raw_input_bayer14,
    size_t raw_image_size,
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out,
    int * rc_out,
    llrpGpuPlaybackReconTiming_t * timing_out)
{
    (void)state;
    (void)raw_input_bayer14;
    (void)raw_image_size;
    if(retained_out) memset(retained_out, 0, sizeof(*retained_out));
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    return 0;
}

int llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(uint64_t token);
int llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(uint64_t token)
{
    (void)token;
    return 1;
}

int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,
                                                         int * rc_out);
int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,
                                                         int * rc_out)
{
    (void)gl_texture_id;
    if(rc_out) *rc_out = -1;
    return 0;
}

int llrpGpuPlaybackReconRunCpu16Probe(const llrpGpuPlaybackReconState_t * state,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size,
                                      uint16_t * output_bayer16,
                                      int * rc_out,
                                      llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunCpu16Probe(const llrpGpuPlaybackReconState_t * state,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size,
                                      uint16_t * output_bayer16,
                                      int * rc_out,
                                      llrpGpuPlaybackReconTiming_t * timing_out)
{
    (void)state;
    (void)raw_input_bayer14;
    (void)raw_image_size;
    (void)output_bayer16;
    if(rc_out) *rc_out = -1;
    if(timing_out) memset(timing_out, 0, sizeof(*timing_out));
    return 0;
}

int llrpGpuPlaybackReconGetBackendInfo(llrpGpuPlaybackReconBackendInfo_t * info);
int llrpGpuPlaybackReconGetBackendInfo(llrpGpuPlaybackReconBackendInfo_t * info)
{
    if(!info) return 0;
    memset(info, 0, sizeof(*info));
    info->unavailable = 1;
    snprintf(info->requested_backend,
             sizeof(info->requested_backend),
             "%s",
             "cuda");
    return 1;
}

int llrpGpuPlaybackReconResetGlTextureResources(void);
int llrpGpuPlaybackReconResetGlTextureResources(void)
{
    return 0;
}
#endif

static int llrawproc_worker_copy_pixel_map(pixel_map * destination,
                                           const pixel_map * source)
{
    if (!destination || !source)
    {
        return 0;
    }

    destination->type = source->type;
    destination->count = source->count;

    if (!source->pixels || source->count == 0)
    {
        return 1;
    }

    if (destination->capacity < source->count)
    {
        pixel_xy * resized = realloc(destination->pixels, source->count * sizeof(pixel_xy));
        if (!resized)
        {
            destination->count = 0;
            return 0;
        }
        destination->pixels = resized;
        destination->capacity = source->count;
    }

    memcpy(destination->pixels, source->pixels, source->count * sizeof(pixel_xy));
    g_llrawproc_debug_pixel_map_copy_count++;
    return 1;
}

static int llrawproc_worker_ensure_u16_copy(uint16_t ** destination,
                                            uint32_t * capacity_bytes,
                                            const uint16_t * source,
                                            uint32_t source_bytes)
{
    if (!destination || !capacity_bytes)
    {
        return 0;
    }

    if (!source || source_bytes == 0)
    {
        return 1;
    }

    if (*capacity_bytes < source_bytes)
    {
        uint16_t * resized = realloc(*destination, source_bytes);
        if (!resized)
        {
            return 0;
        }
        *destination = resized;
        *capacity_bytes = source_bytes;
    }

    memcpy(*destination, source, source_bytes);
    return 1;
}

static void llrawproc_free_worker_state(llrawprocWorkerState_t * worker)
{
    if (!worker) return;

    free_luts(worker->raw2ev, worker->ev2raw);
    worker->raw2ev = NULL;
    worker->ev2raw = NULL;
    worker->prev_black_level = -1;
    free(worker->focus_pixel_map_copy.pixels);
    memset(&worker->focus_pixel_map_copy, 0, sizeof(worker->focus_pixel_map_copy));
    free(worker->bad_pixel_map_copy.pixels);
    memset(&worker->bad_pixel_map_copy, 0, sizeof(worker->bad_pixel_map_copy));
    worker->focus_pixel_map_version = 0;
    worker->bad_pixel_map_version = 0;
    free(worker->dark_frame_data_copy);
    worker->dark_frame_data_copy = NULL;
    worker->dark_frame_size = 0;
    worker->dark_frame_capacity = 0;
    worker->dark_frame_version = 0;
    memset(&worker->dark_frame_hdr_copy, 0, sizeof(worker->dark_frame_hdr_copy));

    free(worker->chroma_smooth_scratch.buffer);
    worker->chroma_smooth_scratch.buffer = NULL;
    worker->chroma_smooth_scratch.capacity = 0;

    free_pattern_noise_scratch(&worker->pattern_noise_scratch);
    free_vertical_stripes_scratch(&worker->vertical_stripes_scratch);

    free(worker->diso_preview_scratch.data_x);
    free(worker->diso_preview_scratch.data_y);
    free(worker->diso_preview_scratch.data_w);
    free(worker->diso_preview_scratch.output_image);
    for(int i = 0; i < 4; i++) hist_destroy(worker->diso_preview_scratch.histograms[i]);
    memset(&worker->diso_preview_scratch, 0, sizeof(worker->diso_preview_scratch));

    free_dualiso_full20bit_scratch(&worker->diso_full20bit_scratch);
    memset(&worker->diso_full20bit_scratch, 0, sizeof(worker->diso_full20bit_scratch));

    free(worker->dual_iso_failure_backup);
    worker->dual_iso_failure_backup = NULL;
    worker->dual_iso_failure_backup_capacity_bytes = 0;

    worker->dng_bit_depth = 0;
    worker->dng_black_level = 0;
    worker->dng_white_level = 0;
}

void llrpInitWorkerState(llrawprocWorkerState_t * worker)
{
    if (!worker) return;

    memset(worker, 0, sizeof(*worker));
    worker->prev_black_level = -1;
}

void llrpFreeWorkerState(llrawprocWorkerState_t * worker)
{
    if (!worker) return;

    llrawproc_free_worker_state(worker);
    llrpInitWorkerState(worker);
}

static uint32_t llrawproc_next_version(uint32_t current_version)
{
    current_version++;
    return current_version ? current_version : 1u;
}

static void llrawproc_bump_focus_map_version(llrawprocObject_t * shared)
{
    if (!shared) return;
    shared->focus_pixel_map_version = llrawproc_next_version(shared->focus_pixel_map_version);
}

static void llrawproc_bump_bad_map_version(llrawprocObject_t * shared)
{
    if (!shared) return;
    shared->bad_pixel_map_version = llrawproc_next_version(shared->bad_pixel_map_version);
}

static const pixel_map * llrawproc_worker_get_focus_map_copy(llrawprocWorkerState_t * worker,
                                                             const llrawprocObject_t * shared)
{
    if (!worker || !shared || shared->fpm_status != 2)
    {
        return NULL;
    }

    if (worker->focus_pixel_map_version != shared->focus_pixel_map_version)
    {
        if (!llrawproc_worker_copy_pixel_map(&worker->focus_pixel_map_copy, &shared->focus_pixel_map))
        {
            return NULL;
        }
        worker->focus_pixel_map_version = shared->focus_pixel_map_version;
    }

    return &worker->focus_pixel_map_copy;
}

static const pixel_map * llrawproc_worker_get_bad_map_copy(llrawprocWorkerState_t * worker,
                                                           const llrawprocObject_t * shared)
{
    if (!worker || !shared || shared->bpm_status != 2)
    {
        return NULL;
    }

    if (worker->bad_pixel_map_version != shared->bad_pixel_map_version)
    {
        if (!llrawproc_worker_copy_pixel_map(&worker->bad_pixel_map_copy, &shared->bad_pixel_map))
        {
            return NULL;
        }
        worker->bad_pixel_map_version = shared->bad_pixel_map_version;
    }

    return &worker->bad_pixel_map_copy;
}

static int llrawproc_worker_sync_dark_frame_copy(llrawprocWorkerState_t * worker,
                                                 const llrawprocObject_t * shared)
{
    if (!worker || !shared)
    {
        return 0;
    }

    if (worker->dark_frame_version == shared->dark_frame_version)
    {
        return 1;
    }

    if (!shared->dark_frame_data || shared->dark_frame_size == 0)
    {
        worker->dark_frame_size = 0;
        memset(&worker->dark_frame_hdr_copy, 0, sizeof(worker->dark_frame_hdr_copy));
        worker->dark_frame_version = shared->dark_frame_version;
        return 1;
    }

    if (!llrawproc_worker_ensure_u16_copy(&worker->dark_frame_data_copy,
                                          &worker->dark_frame_capacity,
                                          shared->dark_frame_data,
                                          shared->dark_frame_size))
    {
        return 0;
    }

    worker->dark_frame_size = shared->dark_frame_size;
    worker->dark_frame_hdr_copy = shared->dark_frame_hdr;
    worker->dark_frame_version = shared->dark_frame_version;
    g_llrawproc_debug_dark_frame_copy_count++;
    return 1;
}

static void llrawproc_worker_reset_dng_bw_levels(llrawprocWorkerState_t * worker,
                                                 const struct raw_info * raw_info)
{
    if (!worker || !raw_info) return;

    worker->dng_bit_depth = raw_info->bits_per_pixel;
    worker->dng_black_level = raw_info->black_level;
    worker->dng_white_level = raw_info->white_level;
}

static llrawproc_runtime_state_t llrawproc_capture_worker_runtime_state(const llrawprocWorkerState_t * worker)
{
    llrawproc_runtime_state_t state = { 0 };
    if (!worker) return state;

    state.diso_pattern = worker->diso_pattern;
    state.diso_auto_correction = worker->diso_auto_correction;
    state.diso_ev_correction = worker->diso_ev_correction;
    state.diso_black_delta = worker->diso_black_delta;
    state.dng_bit_depth = worker->dng_bit_depth;
    state.dng_black_level = worker->dng_black_level;
    state.dng_white_level = worker->dng_white_level;
    return state;
}

static void llrawproc_restore_worker_runtime_state(
    llrawprocWorkerState_t * worker,
    const llrawproc_runtime_state_t * state)
{
    if (!worker || !state) return;

    worker->diso_pattern = state->diso_pattern;
    worker->diso_auto_correction = state->diso_auto_correction;
    worker->diso_ev_correction = state->diso_ev_correction;
    worker->diso_black_delta = state->diso_black_delta;
    worker->dng_bit_depth = state->dng_bit_depth;
    worker->dng_black_level = state->dng_black_level;
    worker->dng_white_level = state->dng_white_level;
    worker->prev_black_level = -1;
}

static uint16_t * llrawproc_worker_capture_dual_iso_failure_backup(
    llrawprocWorkerState_t * worker,
    const uint16_t * source,
    size_t source_bytes)
{
    if (!worker || !source || source_bytes == 0)
    {
        return NULL;
    }

    if (worker->dual_iso_failure_backup_capacity_bytes < source_bytes)
    {
        uint16_t * resized = (uint16_t *)realloc(worker->dual_iso_failure_backup,
                                                 source_bytes);
        if (!resized)
        {
            return NULL;
        }
        worker->dual_iso_failure_backup = resized;
        worker->dual_iso_failure_backup_capacity_bytes = source_bytes;
    }

    memcpy(worker->dual_iso_failure_backup, source, source_bytes);
    return worker->dual_iso_failure_backup;
}

static llrawproc_runtime_state_t llrawproc_capture_shared_runtime_state(const llrawprocObject_t * shared)
{
    llrawproc_runtime_state_t state = { 0 };
    if (!shared) return state;

    state.diso_pattern = shared->diso_pattern;
    state.diso_auto_correction = shared->diso_auto_correction;
    state.diso_ev_correction = shared->diso_ev_correction;
    state.diso_black_delta = shared->diso_black_delta;
    state.dng_bit_depth = shared->dng_bit_depth;
    state.dng_black_level = shared->dng_black_level;
    state.dng_white_level = shared->dng_white_level;
    return state;
}

static int llrawproc_runtime_state_equal(const llrawproc_runtime_state_t * lhs,
                                         const llrawproc_runtime_state_t * rhs,
                                         int compare_auto_correction)
{
    if (!lhs || !rhs) return 0;

    if (lhs->diso_pattern != rhs->diso_pattern) return 0;
    if (compare_auto_correction && lhs->diso_auto_correction != rhs->diso_auto_correction) return 0;
    if (lhs->diso_ev_correction != rhs->diso_ev_correction) return 0;
    if (lhs->diso_black_delta != rhs->diso_black_delta) return 0;
    if (lhs->dng_bit_depth != rhs->dng_bit_depth) return 0;
    if (lhs->dng_black_level != rhs->dng_black_level) return 0;
    if (lhs->dng_white_level != rhs->dng_white_level) return 0;
    return 1;
}

static void llrawproc_worker_ensure_luts(llrawprocWorkerState_t * worker, int32_t black_level)
{
    if (!worker) return;

    if (worker->prev_black_level == black_level && worker->raw2ev && worker->ev2raw)
    {
        return;
    }

    free_luts(worker->raw2ev, worker->ev2raw);
    worker->raw2ev = get_raw2ev(black_level);
    worker->ev2raw = get_ev2raw(black_level);
    worker->prev_black_level = black_level;
}

static llrawprocWorkerState_t * llrawproc_acquire_worker_state(mlvObject_t * video)
{
    if (!video) return NULL;

    pthread_t thread_id = pthread_self();
    llrawprocWorkerState_t * slot = NULL;

    pthread_mutex_lock(&video->llrawproc_worker_mutex);

    if (!video->llrawproc_workers)
    {
        const uint32_t initial_capacity = (uint32_t)MAX(video->cpu_cores + 4, 16);
        video->llrawproc_workers = calloc(initial_capacity, sizeof(llrawprocWorkerState_t));
        if (video->llrawproc_workers)
        {
            video->llrawproc_worker_capacity = initial_capacity;
        }
    }

    for (uint32_t i = 0; i < video->llrawproc_worker_capacity; ++i)
    {
        if (video->llrawproc_workers[i].in_use
         && pthread_equal(video->llrawproc_workers[i].thread_id, thread_id))
        {
            slot = &video->llrawproc_workers[i];
            break;
        }
    }

    if (!slot)
    {
        for (uint32_t i = 0; i < video->llrawproc_worker_capacity; ++i)
        {
            if (!video->llrawproc_workers[i].in_use)
            {
                slot = &video->llrawproc_workers[i];
                memset(slot, 0, sizeof(*slot));
                slot->in_use = 1;
                slot->thread_id = thread_id;
                slot->prev_black_level = -1;
                break;
            }
        }
    }

    if (!slot)
    {
#ifndef STDOUT_SILENT
        fprintf(stderr, "llrawproc: worker pool exhausted, using stack scratch for this call.\n");
#endif
    }

    pthread_mutex_unlock(&video->llrawproc_worker_mutex);
    return slot;
}

static void llrawproc_publish_worker_results(mlvObject_t * video,
                                             const llrawproc_runtime_state_t * runtime_state,
                                             int publish_auto_correction)
{
    if (!video || !video->llrawproc || !runtime_state) return;

    video->llrawproc->diso_pattern = runtime_state->diso_pattern;
    if (publish_auto_correction)
    {
        video->llrawproc->diso_auto_correction = runtime_state->diso_auto_correction;
    }
    video->llrawproc->diso_ev_correction = runtime_state->diso_ev_correction;
    video->llrawproc->diso_black_delta = runtime_state->diso_black_delta;
    video->llrawproc->dng_bit_depth = runtime_state->dng_bit_depth;
    video->llrawproc->dng_black_level = runtime_state->dng_black_level;
    video->llrawproc->dng_white_level = runtime_state->dng_white_level;
    g_llrawproc_debug_runtime_publish_count++;
}

static void llrawproc_reset_force_bad_pixel_search(mlvObject_t * video, int bad_pixels)
{
    if (!video || !video->llrawproc || bad_pixels != 2) return;

    const double reset_lock_start = mlv_stage_timing_now();
    pthread_mutex_lock(&video->llrawproc_mutex);
    if (video->llrawproc->bpm_status == 2)
    {
        video->llrawproc->bpm_status = 1;
        video->llrawproc->bad_pixel_map.count = 0;
    }
    pthread_mutex_unlock(&video->llrawproc_mutex);
    g_llrawproc_last_shared_lock_ms += (mlv_stage_timing_now() - reset_lock_start) * 1000.0;
#ifndef STDOUT_SILENT
    printf("Searching bad pixels for every frame\n");
#endif
}

/* this is DNG feature only */
static void deflicker(mlvObject_t * video, uint16_t * raw_image_buff, size_t raw_image_size)
{
    uint16_t black = video->RAWI.raw_info.black_level;
    uint16_t white = (1 << video->RAWI.raw_info.bits_per_pixel) - 1;

    struct histogram * hist = hist_create(white);
    hist_add(hist, raw_image_buff + 1, (uint32_t)((raw_image_size - 1) / 2), 1);
    uint16_t median = hist_median(hist);
    double correction = log2((double) (video->llrawproc->deflicker_target - black) / (median - black));
    video->RAWI.raw_info.exposure_bias[0] = correction * 10000;
    video->RAWI.raw_info.exposure_bias[1] = 10000;
}

static int llrawproc_checked_14bit_frame_extents(int width,
                                                int height,
                                                size_t * pixel_count,
                                                uint32_t * frame_size)
{
    if (!pixel_count || !frame_size || width <= 0 || height <= 0)
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
    if (checked_pixels > SIZE_MAX / 14u)
    {
        return 0;
    }
    const size_t checked_frame_size = checked_pixels * 14u / 8u;
    if (checked_frame_size > UINT32_MAX)
    {
        return 0;
    }

    *pixel_count = checked_pixels;
    *frame_size = (uint32_t)checked_frame_size;
    return 1;
}

/* Production-linked arithmetic seam; it performs no allocation or frame
 * access and lets boundary tests exercise otherwise impractical dimensions. */
int llrawprocChecked14BitFrameSizeForTesting(int width,
                                             int height,
                                             uint32_t * frame_size);
int llrawprocChecked14BitFrameSizeForTesting(int width,
                                             int height,
                                             uint32_t * frame_size)
{
    size_t pixel_count = 0;
    return llrawproc_checked_14bit_frame_extents(width,
                                                height,
                                                &pixel_count,
                                                frame_size);
}

/* convert uncompressed 10/12bit raw data to 14bit for subsequent processing */
static int make_14bit(uint16_t * raw_image_buff, size_t raw_image_size, struct raw_info * raw_info)
{
    size_t expected_pixel_count = 0;
    uint32_t frame_size = 0;
    if (!raw_image_buff || !raw_info
        || raw_info->bits_per_pixel <= 0 || raw_info->bits_per_pixel >= 14
        || !llrawproc_checked_14bit_frame_extents(raw_info->width,
                                                  raw_info->height,
                                                  &expected_pixel_count,
                                                  &frame_size)
        || expected_pixel_count > SIZE_MAX / sizeof(uint16_t)
        || expected_pixel_count * sizeof(uint16_t) != raw_image_size)
    {
        return 0;
    }

    const int bits_shift = 14 - raw_info->bits_per_pixel;
    const int level_scale = 1 << bits_shift;
    if (raw_info->black_level < 0 || raw_info->white_level < 0
        || raw_info->black_level > INT_MAX / level_scale
        || raw_info->white_level > INT_MAX / level_scale)
    {
        return 0;
    }

    raw_info->black_level *= level_scale;
    raw_info->white_level *= level_scale;
    raw_info->bits_per_pixel = 14;
    raw_info->frame_size = frame_size;

    #pragma omp parallel for
    for(size_t i = 0; i < expected_pixel_count; ++i)
    {
        raw_image_buff[i] <<= bits_shift;
    }
    return 1;
}

/* undo 14bit conversion to initial bit depth with rounding error minimizing */
static void undo_14bit(uint16_t * raw_image_buff, size_t raw_image_size, uint32_t bpp)
{
    uint32_t pixel_count = raw_image_size / 2;
    int bits_shift = 14 - bpp;
    /* calculate rounding number to be added to the raw value before shifting right to minimize rounding error */
    uint32_t rounding_number = (uint32_t)pow(2, bits_shift - 1);

    #pragma omp parallel for
    for(uint32_t i = 0; i < pixel_count; ++i)
    {
        raw_image_buff[i] = (raw_image_buff[i] + rounding_number) >> bits_shift;
    }
}

/* rescale restricted to imaginary 10-12bit levels of lossless raw data to about real 14bit range */
static int scale_restricted_range(struct raw_info * raw_info,
                                  uint16_t * image_data,
                                  size_t pixel_count,
                                  int low_iso,
                                  int high_iso)
{
    if (!raw_info || !image_data || pixel_count == 0
        || raw_info->width <= 0 || raw_info->height <= 0)
    {
        return 0;
    }

    const size_t width = (size_t)raw_info->width;
    const size_t height = (size_t)raw_info->height;
    if (width > SIZE_MAX / height || width * height != pixel_count
        || raw_info->black_level < 0 || raw_info->black_level > 16383
        || raw_info->white_level < 0 || raw_info->white_level > 16383
        || raw_info->white_level <= raw_info->black_level)
    {
        return 0;
    }

    const int32_t signal_span = raw_info->white_level - raw_info->black_level;
    const double bd_value = ceil(log2((double)signal_span));
    if (!isfinite(bd_value) || bd_value < 0.0 || bd_value > 14.0)
    {
        return 0;
    }
    const int32_t bd = (int32_t)bd_value;

    // Digital gain? Add 1 bit…
    int32_t add_bit = 0;

    if (low_iso != high_iso && high_iso >= 6400)
    {
        add_bit = 1;
    }

    const int32_t actual_shift = bd + add_bit;
    const int32_t scaled_shift = 14 - bd;
    if (actual_shift < 0 || actual_shift > 15
        || scaled_shift < 0 || scaled_shift > 14)
    {
        return 0;
    }

    const uint32_t actual_span = (1u << (uint32_t)actual_shift) - 1u;
    const uint32_t scaled_factor = 1u << (uint32_t)scaled_shift;
    const int64_t actual_white_wide = (int64_t)raw_info->black_level + actual_span;
    const int64_t scaled_white_wide = (int64_t)signal_span * scaled_factor;
    if (actual_white_wide <= raw_info->black_level || actual_white_wide > INT_MAX
        || scaled_white_wide <= raw_info->black_level || scaled_white_wide > 16383)
    {
        return 0;
    }

    const int32_t actual_white_level = (int32_t)actual_white_wide;
    const int32_t scaled_white_level = (int32_t)scaled_white_wide;

    const double scale_ratio =
        (double)(scaled_white_level - raw_info->black_level)
        / (double)(actual_white_level - raw_info->black_level);
    if (!isfinite(scale_ratio) || scale_ratio <= 0.0)
    {
        return 0;
    }

    #pragma omp parallel for
    for (size_t i = 0; i < pixel_count; ++i)
    {
        const double scaled =
            (double)(image_data[i] - raw_info->black_level) * scale_ratio
            + raw_info->black_level;
        image_data[i] = scaled <= 0.0
                      ? 0
                      : scaled >= 16383.0
                      ? 16383
                      : (uint16_t)(scaled + 0.5);
    }
    raw_info->white_level = scaled_white_level;
    return 1;
}

/* Production-linked fail-closed seam for hostile arithmetic tests. */
int llrawprocScaleRestrictedRangeForTesting(struct raw_info * raw_info,
                                            uint16_t * image_data,
                                            size_t pixel_count,
                                            int low_iso,
                                            int high_iso);
int llrawprocScaleRestrictedRangeForTesting(struct raw_info * raw_info,
                                            uint16_t * image_data,
                                            size_t pixel_count,
                                            int low_iso,
                                            int high_iso)
{
    return scale_restricted_range(raw_info,
                                  image_data,
                                  pixel_count,
                                  low_iso,
                                  high_iso);
}

/* initialise low level raw processing struct */
llrawprocObject_t * initLLRawProcObject()
{
    llrawprocObject_t * llrawproc = calloc(1, sizeof(llrawprocObject_t));

    /* set defaults */
    llrawproc->vertical_stripes = 1;
    llrawproc->focus_pixels = 0;
    llrawproc->fpi_method = 0;
    llrawproc->bad_pixels = 1;
    llrawproc->bps_method = 0;
    llrawproc->bpi_method = 0;
    llrawproc->chroma_smooth = 0;
    llrawproc->pattern_noise = 0;
    llrawproc->deflicker_target = 0;
    llrawproc->fpm_status = 0;
    llrawproc->bpm_status = 0;
    llrawproc->compute_stripes = 0;
    llrawproc->dual_iso = 0;
    llrawproc->diso_pattern = 0;
    llrawproc->diso_auto_correction = -1;
    llrawproc->diso_ev_correction = 0;
    llrawproc->diso_black_delta = 0;
    llrawproc->diso_averaging = 0;
    llrawproc->diso_playback_force_mean23 = 0;
    llrawproc->diso_alias_map = 0;
    llrawproc->diso_frblending = 1;
    llrawproc->diso_playback_force_disable_alias_map = 0;
    llrawproc->playback_pre_dualiso_fix_ms = 0.0;
    llrawproc->dark_frame = 0;

    llrawproc->dark_frame_filename = NULL;
    llrawproc->dark_frame_loaded_filename = NULL;
    llrawproc->dark_frame_loaded_mode = DF_OFF;
    llrawproc->dark_frame_data = NULL;
    llrawproc->dark_frame_size = 0;
    llrawproc->dark_frame_version = 1;

    llrawproc->raw2ev = NULL;
    llrawproc->ev2raw = NULL;

    llrawproc->prev_black_level = -1;

    llrawproc->focus_pixel_map.type = PIX_FOCUS;
    llrawproc->focus_pixel_map.pixels = NULL;
    llrawproc->focus_pixel_map_version = 1;
    llrawproc->bad_pixel_map.type = PIX_BAD;
    llrawproc->bad_pixel_map.pixels = NULL;
    llrawproc->bad_pixel_map_version = 1;

    return llrawproc;
}

void freeLLRawProcObject(mlvObject_t * video)
{
    df_free_filename(video);
    df_free(video);

    if (video->llrawproc_workers)
    {
        for (uint32_t i = 0; i < video->llrawproc_worker_capacity; ++i)
        {
            if (video->llrawproc_workers[i].in_use)
            {
                llrawproc_free_worker_state(&video->llrawproc_workers[i]);
            }
        }
        free(video->llrawproc_workers);
        video->llrawproc_workers = NULL;
        video->llrawproc_worker_capacity = 0;
    }

    free_luts(video->llrawproc->raw2ev, video->llrawproc->ev2raw);
    free(video->llrawproc->chroma_smooth_scratch.buffer);
    free_pattern_noise_scratch(&video->llrawproc->pattern_noise_scratch);
    free(video->llrawproc->diso_preview_scratch.data_x);
    free(video->llrawproc->diso_preview_scratch.data_y);
    free(video->llrawproc->diso_preview_scratch.data_w);
    free(video->llrawproc->diso_preview_scratch.output_image);
    for(int i = 0; i < 4; i++) hist_destroy(video->llrawproc->diso_preview_scratch.histograms[i]);
    free_dualiso_full20bit_scratch(&video->llrawproc->diso_full20bit_scratch);
    free_pixel_maps(&(video->llrawproc->focus_pixel_map), &(video->llrawproc->bad_pixel_map));
    free(video->llrawproc);
}

/* all low level raw processing takes place here */
void applyLLRawProcObjectWorker(mlvObject_t * video,
                                uint16_t * raw_image_buff,
                                size_t raw_image_size,
                                llrawprocWorkerState_t * supplied_worker,
                                int stop_before_dual_iso)
{
    const double apply_start = mlv_stage_timing_now();
    llrawprocObject_t * shared = video ? video->llrawproc : NULL;
    const int isolated_analysis = g_llrawproc_analysis_isolation_enabled;
    const int llrawproc_threads =
        llrawproc_analysis_thread_count(video, isolated_analysis);
    llrawprocWorkerState_t stack_worker;
    llrawprocWorkerState_t * worker = supplied_worker;
    int using_stack_worker = 0;

    llrawproc_gpu_export_reset_last_run_state();
    llrawproc_gpu_export_set_skip_code((!isolated_analysis && llrawproc_gpu_export_enabled())
        ? LLRP_GPU_EXPORT_SKIP_NOT_DUAL_ISO
        : LLRP_GPU_EXPORT_SKIP_DISABLED);
    g_llrawproc_last_shared_lock_ms = 0.0;
    g_llrawproc_last_dualiso_refine_lock_ms = 0.0;
    g_llrawproc_last_publish_lock_ms = 0.0;
    g_llrawproc_last_total_ms = 0.0;
    g_llrawproc_last_dark_frame_ms = 0.0;
    g_llrawproc_last_vertical_stripes_ms = 0.0;
    g_llrawproc_last_focus_pixels_ms = 0.0;
    g_llrawproc_last_bad_pixels_ms = 0.0;
    g_llrawproc_last_pattern_noise_ms = 0.0;
    g_llrawproc_last_pre_dualiso_fix_ms = 0.0;
    g_llrawproc_last_dual_iso_ms = 0.0;
    g_llrawproc_last_chroma_smooth_ms = 0.0;
    llrawproc_reset_dual_iso_full20bit_timing();
    g_llrawproc_last_preview_histogram_ms = 0.0;
    g_llrawproc_last_preview_regression_ms = 0.0;
    g_llrawproc_last_preview_rowscale_ms = 0.0;
    llrawproc_gpu_playback_reset_last_run_state();
    g_llrawproc_debug_pixel_map_copy_count = 0;
    g_llrawproc_debug_dark_frame_copy_count = 0;
    if (!video || !shared || !shared->fix_raw)
    {
        g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
        return;
    }

    double dark_frame_ms = 0.0;
    double vertical_stripes_ms = 0.0;
    double focus_pixels_ms = 0.0;
    double bad_pixels_ms = 0.0;
    double pattern_noise_ms = 0.0;
    double dual_iso_ms = 0.0;
    double chroma_smooth_ms = 0.0;

    if (worker && !worker->raw2ev && !worker->ev2raw && worker->prev_black_level == 0)
    {
        worker->prev_black_level = -1;
    }
    if (!worker)
    {
        memset(&stack_worker, 0, sizeof(stack_worker));
        stack_worker.prev_black_level = -1;
        worker = llrawproc_acquire_worker_state(video);
        if (!worker)
        {
            worker = &stack_worker;
            using_stack_worker = 1;
        }
    }

    struct raw_info raw_info = video->RAWI.raw_info;
    const int original_bits_per_pixel = video->RAWI.raw_info.bits_per_pixel;
    const int x_res = video->RAWI.xRes;
    const int y_res = video->RAWI.yRes;
    const int camera_id = video->IDNT.cameraModel;
    const int pan_pos_x = video->VIDF.panPosX;
    const int pan_pos_y = video->VIDF.panPosY;
    const int raw_width = video->RAWI.raw_info.width;
    const int raw_height = video->RAWI.raw_info.height;
    const int crop_rec_mode = (llrpDetectFocusDotFixMode(video) == 2) ? 1 : 0;
    const int unified_mode = (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) ? 5 : 0;

    int focus_pixels = 0;
    int fpi_method = 0;
    int bad_pixels = 0;
    int bps_method = 0;
    int bpi_method = 0;
    int chroma_smooth_mode = 0;
    int pattern_noise_mode = 0;
    int diso_validity = 0;
    int dual_iso_mode = 0;
    int diso1 = 0;
    int diso2 = 0;
    int diso_averaging = 0;
    int diso_alias_map = 0;
    int diso_frblending = 0;
    int dark_frame_mode = 0;
    int vertical_stripes_mode = 0;
    int worker_diso_pattern = 0;
    int worker_diso_auto_correction = 0;
    double worker_diso_ev_correction = 0.0;
    int worker_diso_black_delta = 0;
    int apply_dark_frame_outside_lock = 0;
    const uint16_t * dark_frame_data_for_subtraction = NULL;
    uint32_t dark_frame_size_for_subtraction = 0;
    uint32_t dark_frame_black_level = 0;
    const pixel_map * focus_map_for_interpolation = NULL;
    const pixel_map * bad_map_for_interpolation = NULL;
    int focus_status_snapshot = 0;
    int bad_status_snapshot = 0;
    int focus_interpolate_outside_lock = 0;
    int bad_interpolate_outside_lock = 0;
    int bad_force_reset_after_interpolation = 0;
    int apply_vertical_stripes_outside_lock = 0;
    stripes_correction stripe_correction_snapshot = { 0 };
    int compute_vertical_stripes_outside_lock = 0;
    int claimed_vertical_stripes_request = 0;
    int vertical_stripes_compute_succeeded = 0;

    if (original_bits_per_pixel < 14)
    {
        if (!make_14bit(raw_image_buff, raw_image_size, &raw_info))
        {
            if (using_stack_worker) llrawproc_free_worker_state(worker);
            return;
        }
    }

    llrawproc_worker_reset_dng_bw_levels(worker, &raw_info);
    llrawproc_worker_ensure_luts(worker, raw_info.black_level);

    double shared_lock_start = mlv_stage_timing_now();
    pthread_mutex_lock(&video->llrawproc_mutex);

    if (!shared->fix_raw)
    {
        pthread_mutex_unlock(&video->llrawproc_mutex);
        g_llrawproc_last_shared_lock_ms = (mlv_stage_timing_now() - shared_lock_start) * 1000.0;
        g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
        if (using_stack_worker) llrawproc_free_worker_state(worker);
        return;
    }

    if (!df_init(video))
    {
        const double dark_frame_start = mlv_stage_timing_now();
        if (llrawproc_worker_sync_dark_frame_copy(worker, shared)
         && worker->dark_frame_data_copy
         && worker->dark_frame_size == raw_image_size)
        {
            apply_dark_frame_outside_lock = 1;
            dark_frame_data_for_subtraction = worker->dark_frame_data_copy;
            dark_frame_size_for_subtraction = worker->dark_frame_size;
            dark_frame_black_level = worker->dark_frame_hdr_copy.black_level;
        }
        else
        {
#ifndef STDOUT_SILENT
            printf("Subtracting Dark Frame... ");
#endif
            df_subtract(video, raw_image_buff, raw_image_size);
#ifndef STDOUT_SILENT
            printf("Done\n\n");
#endif
        }
        dark_frame_ms += (mlv_stage_timing_now() - dark_frame_start) * 1000.0;
    }

    focus_pixels = shared->focus_pixels;
    fpi_method = shared->fpi_method;
    bad_pixels = shared->bad_pixels;
    bps_method = shared->bps_method;
    bpi_method = shared->bpi_method;
    chroma_smooth_mode =
        llrawproc_analysis_chroma_smooth_method(shared->chroma_smooth,
                                                isolated_analysis);
    pattern_noise_mode = shared->pattern_noise;
    diso_validity = shared->diso_validity;
    dual_iso_mode = shared->dual_iso;
    diso1 = shared->diso1;
    diso2 = shared->diso2;
    diso_averaging = shared->diso_averaging;
    /* Playback-only fast-path override: when set by the GUI playback policy
     * (platform/qt/DualIsoPlaybackPolicy.h), force HQ recon (dual_iso == 1)
     * to use mean23 instead of AMaZE. mean23 is also a matched-pair recon,
     * so the cast still closes; AMaZE costs ~150-200 ms p95 on 5K dual ISO,
     * mean23 costs ~30-50 ms. The receipt's authored diso_averaging is
     * preserved (the override only reads the flag here, never writes the
     * shared field) so paused/scrubbing/export keep AMaZE.
     *
     * Cache invalidation: diso_playback_force_mean23 is hashed by
     * mlv_hash_llrawproc_state, so the processed-frame cache slot signature
     * differs between playback-active (override=1) and paused (override=0)
     * — the same frame index produces two cache slots, and switching from
     * playback to paused presents the AMaZE pixels not the mean23 ones. */
    if (!isolated_analysis
        && shared->diso_playback_force_mean23 != 0
        && !dualiso_playback_mean23_override_disabled_via_env())
    {
        diso_averaging = 1; /* DISOI_MEAN23 */
    }
    diso_alias_map = shared->diso_alias_map;
    diso_frblending = shared->diso_frblending;
    /* Phase E5 playback-only fast-path override: at scale >= 4 the GUI
     * policy disables alias_map suppression because the 4x4 downsample is
     * itself an anti-aliasing operation. That stage costs ~4-9 ms/frame on
     * 5K dual-ISO clips. Receipt-authored values are not modified, so
     * paused/scrubbing/export keep diso_alias_map untouched. Cache
     * invalidation: the override field is hashed by
     * mlv_hash_llrawproc_state, so playback-active and paused produce
     * different cache slot signatures for the same frame index. */
    if (!isolated_analysis
        && !dualiso_playback_alias_map_downgrade_disabled_via_env())
    {
        if (shared->diso_playback_force_disable_alias_map != 0)
        {
            diso_alias_map = 0;
        }
    }
    worker_diso_pattern = shared->diso_pattern;
    worker_diso_auto_correction = shared->diso_auto_correction;
    worker_diso_ev_correction = shared->diso_ev_correction;
    worker_diso_black_delta = shared->diso_black_delta;
    worker->seeded_runtime_state = llrawproc_capture_shared_runtime_state(shared);
    if (isolated_analysis)
    {
        llrawproc_runtime_state_t analysis_seed = { 0 };
        worker_diso_pattern = 0;
        worker_diso_auto_correction = -1;
        worker_diso_ev_correction = 1.0;
        worker_diso_black_delta = -1;
        analysis_seed.diso_auto_correction = -1;
        analysis_seed.diso_ev_correction = 1.0;
        analysis_seed.diso_black_delta = -1;
        worker->seeded_runtime_state = analysis_seed;
    }
    dark_frame_mode = shared->dark_frame;
    vertical_stripes_mode = shared->vertical_stripes;

    if (vertical_stripes_mode)
    {
        stripe_correction_snapshot = shared->stripe_corrections;
        compute_vertical_stripes_outside_lock = (shared->compute_stripes || vertical_stripes_mode == 2);
        if (shared->compute_stripes)
        {
            /* Claim the queued recompute before unlock so only one worker
               solves/publishes a one-shot request. Forced mode still
               recomputes every frame via vertical_stripes_mode == 2. */
            claimed_vertical_stripes_request = 1;
            shared->compute_stripes = 0;
        }
        if (!compute_vertical_stripes_outside_lock)
        {
            apply_vertical_stripes_outside_lock = stripe_correction_snapshot.correction_needed;
        }
    }

    if (compute_vertical_stripes_outside_lock)
    {
        pthread_mutex_unlock(&video->llrawproc_mutex);
        g_llrawproc_last_shared_lock_ms = (mlv_stage_timing_now() - shared_lock_start) * 1000.0;

        const double vertical_stripes_start = mlv_stage_timing_now();
        vertical_stripes_compute_succeeded = compute_vertical_stripes_correction_only(&stripe_correction_snapshot,
                                                                                      raw_image_buff,
                                                                                      raw_info.black_level,
                                                                                      raw_info.white_level,
                                                                                      raw_info.frame_size,
                                                                                      x_res,
                                                                                      y_res,
                                                                                      vertical_stripes_mode,
                                                                                      &worker->vertical_stripes_scratch);
        vertical_stripes_ms += (mlv_stage_timing_now() - vertical_stripes_start) * 1000.0;

        const double stripes_publish_lock_start = mlv_stage_timing_now();
        pthread_mutex_lock(&video->llrawproc_mutex);
        if (vertical_stripes_compute_succeeded)
        {
            shared->stripe_corrections = stripe_correction_snapshot;
        }
        else if (claimed_vertical_stripes_request)
        {
            shared->compute_stripes = 1;
        }
        const double stripes_publish_lock_end = mlv_stage_timing_now();
        g_llrawproc_last_shared_lock_ms += (stripes_publish_lock_end - stripes_publish_lock_start) * 1000.0;
        shared_lock_start = stripes_publish_lock_end;
        stripe_correction_snapshot = shared->stripe_corrections;
        apply_vertical_stripes_outside_lock = stripe_correction_snapshot.correction_needed;
    }

    if (focus_pixels)
    {
        const double focus_pixels_start = mlv_stage_timing_now();
        if (shared->fpm_status < 2)
        {
            int crop_rec = crop_rec_mode ? 1 : (focus_pixels == 2);
            prepare_focus_pixel_map(&shared->focus_pixel_map,
                                    &shared->fpm_status,
                                    camera_id,
                                    raw_width,
                                    raw_height,
                                    crop_rec,
                                    unified_mode);
            llrawproc_bump_focus_map_version(shared);
        }
        focus_status_snapshot = shared->fpm_status;
        if (focus_status_snapshot == 2)
        {
            focus_map_for_interpolation = llrawproc_worker_get_focus_map_copy(worker, shared);
            focus_interpolate_outside_lock = (focus_map_for_interpolation != NULL);
        }
        else focus_interpolate_outside_lock = 0;
        focus_pixels_ms += (mlv_stage_timing_now() - focus_pixels_start) * 1000.0;
    }

    if (bad_pixels)
    {
        const double bad_pixels_start = mlv_stage_timing_now();
        if (shared->bpm_status < 2 || (shared->bpm_status == 2 && bad_pixels == 2))
        {
            bad_force_reset_after_interpolation = prepare_bad_pixel_map(&shared->bad_pixel_map,
                                                                        &shared->bpm_status,
                                                                        raw_image_buff,
                                                                        camera_id,
                                                                        x_res,
                                                                        y_res,
                                                                        pan_pos_x,
                                                                        pan_pos_y,
                                                                        raw_width,
                                                                        raw_height,
                                                                        raw_info.black_level,
                                                                        bad_pixels,
                                                                        bps_method,
                                                                        worker->raw2ev);
            llrawproc_bump_bad_map_version(shared);
        }
        bad_status_snapshot = shared->bpm_status;
        if (bad_status_snapshot == 2)
        {
            bad_map_for_interpolation = llrawproc_worker_get_bad_map_copy(worker, shared);
            bad_interpolate_outside_lock = (bad_map_for_interpolation != NULL);
        }
        else bad_interpolate_outside_lock = 0;
        bad_pixels_ms += (mlv_stage_timing_now() - bad_pixels_start) * 1000.0;
    }

    pthread_mutex_unlock(&video->llrawproc_mutex);
    g_llrawproc_last_shared_lock_ms += (mlv_stage_timing_now() - shared_lock_start) * 1000.0;

    worker->diso_pattern = worker_diso_pattern;
    worker->diso_auto_correction = worker_diso_auto_correction;
    worker->diso_ev_correction = worker_diso_ev_correction;
    worker->diso_black_delta = worker_diso_black_delta;

    if (apply_dark_frame_outside_lock)
    {
        const double dark_frame_start = mlv_stage_timing_now();
        df_subtract_snapshot(dark_frame_data_for_subtraction,
                             dark_frame_size_for_subtraction,
                             dark_frame_black_level,
                             raw_info.bits_per_pixel,
                             raw_image_buff,
                             raw_image_size);
        dark_frame_ms += (mlv_stage_timing_now() - dark_frame_start) * 1000.0;
    }

    if (apply_vertical_stripes_outside_lock)
    {
        const double vertical_stripes_start = mlv_stage_timing_now();
        apply_vertical_stripes_correction_only(&stripe_correction_snapshot,
                                               raw_image_buff,
                                               raw_info.black_level,
                                               raw_info.white_level,
                                               x_res,
                                               y_res);
        vertical_stripes_ms += (mlv_stage_timing_now() - vertical_stripes_start) * 1000.0;
    }

    if (focus_pixels && focus_interpolate_outside_lock && focus_status_snapshot == 2 && focus_map_for_interpolation)
    {
        const double focus_pixels_start = mlv_stage_timing_now();
        interpolate_focus_pixel_map(focus_map_for_interpolation,
                                    raw_image_buff,
                                    x_res,
                                    y_res,
                                    pan_pos_x,
                                    pan_pos_y,
                                    fpi_method,
                                    dual_iso_mode,
                                    worker->raw2ev,
                                    worker->ev2raw);
        focus_pixels_ms += (mlv_stage_timing_now() - focus_pixels_start) * 1000.0;
    }

    if (bad_pixels && bad_interpolate_outside_lock && bad_status_snapshot == 2 && bad_map_for_interpolation)
    {
        const double bad_pixels_start = mlv_stage_timing_now();
        interpolate_bad_pixel_map(bad_map_for_interpolation,
                                  raw_image_buff,
                                  x_res,
                                  y_res,
                                  pan_pos_x,
                                  pan_pos_y,
                                  bpi_method,
                                  dual_iso_mode,
                                  worker->raw2ev,
                                  worker->ev2raw);
        if (bad_force_reset_after_interpolation)
        {
            llrawproc_reset_force_bad_pixel_search(video, bad_pixels);
        }
        bad_pixels_ms += (mlv_stage_timing_now() - bad_pixels_start) * 1000.0;
    }

    if (!diso_validity && pattern_noise_mode)
    {
        const double pattern_noise_start = mlv_stage_timing_now();
#ifndef STDOUT_SILENT
        printf("Fixing pattern noise... ");
#endif
        fix_pattern_noise((int16_t *)raw_image_buff,
                          x_res,
                          y_res,
                          raw_info.white_level,
                          0,
                          &worker->pattern_noise_scratch);
#ifndef STDOUT_SILENT
        printf("Done\n\n");
#endif
        pattern_noise_ms += (mlv_stage_timing_now() - pattern_noise_start) * 1000.0;
    }

    if (stop_before_dual_iso)
    {
        g_llrawproc_last_pre_dualiso_fix_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
        g_llrawproc_last_total_ms = g_llrawproc_last_pre_dualiso_fix_ms;
        if (shared)
        {
            shared->playback_pre_dualiso_fix_ms = g_llrawproc_last_pre_dualiso_fix_ms;
        }
        if (using_stack_worker) llrawproc_free_worker_state(worker);
        return;
    }

    int publish_auto_correction = 1;

    /* S1_pre_dualiso capture: post focus pixel / bad pixel / chroma smooth /
     * pattern noise / dark frame / vertical stripes, but pre Dual ISO recon.
     * Inert when MLVAPP_PIPELINE_CAPTURE_DIR is unset. The current frame
     * index is read from the thread-local set by the caller of
     * applyLLRawProcObject. */
    {
        const uint64_t frame_index = mlv_pipeline_capture_get_current_frame();
        if (mlv_pipeline_capture_should_capture_frame(frame_index))
        {
            mlv_pipeline_capture_meta_t meta;
            memset(&meta, 0, sizeof meta);
            meta.stage = MLV_PIPELINE_STAGE_S1_PRE_DUALISO;
            meta.format = MLV_PIPELINE_FORMAT_UINT16_MONO;
            meta.format_label = "uint16_bayer_pre_dualiso";
            meta.width = x_res;
            meta.height = y_res;
            meta.bytes_per_line = x_res * (int)sizeof(uint16_t);
            meta.bytes_per_pixel = (int)sizeof(uint16_t);
            meta.channels = 1;
            meta.bit_depth = 16;
            meta.dual_iso_mode = (dual_iso_mode == 0) ? "off"
                               : (dual_iso_mode == 1) ? "full"
                               : (dual_iso_mode == 2) ? "preview"
                               : "unknown";
            meta.debayer_mode = "n/a";
            meta.scaler = "none";
            meta.path_label = "applyLLRawProcObject_pre_dualiso";
            mlv_pipeline_capture(frame_index, raw_image_buff, &meta);
        }
    }

    if (diso_validity && dual_iso_mode)
    {
        raw_info.width = x_res;
        raw_info.height = y_res;
        raw_info.pitch = x_res;
        raw_info.active_area.x1 = 0;
        raw_info.active_area.y1 = 0;
        raw_info.active_area.x2 = raw_info.width;
        raw_info.active_area.y2 = raw_info.height;

        uint16_t * dual_iso_failure_backup = NULL;
        int restricted_lossless = (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) && raw_info.white_level < 15000;
        size_t restricted_pixel_count = 0;

        if (restricted_lossless)
        {
            uint32_t restricted_frame_size = 0;
            if (!llrawproc_checked_14bit_frame_extents(raw_info.width,
                                                       raw_info.height,
                                                       &restricted_pixel_count,
                                                       &restricted_frame_size)
                || restricted_pixel_count > SIZE_MAX / sizeof(uint16_t)
                || restricted_pixel_count * sizeof(uint16_t) != raw_image_size)
            {
                if (original_bits_per_pixel < 14)
                {
                    undo_14bit(raw_image_buff,
                               raw_image_size,
                               original_bits_per_pixel);
                }
                if (using_stack_worker) llrawproc_free_worker_state(worker);
                return;
            }
            if (dual_iso_mode == 1)
            {
                dual_iso_failure_backup =
                    llrawproc_worker_capture_dual_iso_failure_backup(
                        worker,
                        raw_image_buff,
                        raw_image_size);
                if (!dual_iso_failure_backup)
                {
                    if (original_bits_per_pixel < 14)
                    {
                        undo_14bit(raw_image_buff,
                                   raw_image_size,
                                   original_bits_per_pixel);
                    }
                    if (using_stack_worker) llrawproc_free_worker_state(worker);
                    return;
                }
            }
            const double dual_iso_start = mlv_stage_timing_now();
#ifndef STDOUT_SILENT
            printf("\nScaling raw data range...\n");
            printf("Raw_Black = %d, Raw_White = %d <= BEFORE SCALING\n", raw_info.black_level, raw_info.white_level);
#endif
            int low_iso = MIN(diso1, diso2);
            int high_iso = MAX(diso1, diso2);
            if (!scale_restricted_range(&raw_info,
                                        raw_image_buff,
                                        restricted_pixel_count,
                                        low_iso,
                                        high_iso))
            {
                if (original_bits_per_pixel < 14)
                {
                    undo_14bit(raw_image_buff,
                               raw_image_size,
                               original_bits_per_pixel);
                }
                if (using_stack_worker) llrawproc_free_worker_state(worker);
                return;
            }
            llrawproc_worker_reset_dng_bw_levels(worker, &raw_info);
#ifndef STDOUT_SILENT
            printf("Raw_Black = %d, Raw_White = %d <= AFTER SCALING\n", raw_info.black_level, raw_info.white_level);
#endif
            dual_iso_ms += (mlv_stage_timing_now() - dual_iso_start) * 1000.0;
        }

        if (dual_iso_mode == 1)
        {
            const double dual_iso_start = mlv_stage_timing_now();
            uint16_t * gpu_export_input = NULL;
            uint16_t * gpu_playback_input = NULL;
            int gpu_playback_input_owned_by_last_snapshot = 0;
            int gpu_playback_input_borrowed_from_raw_image = 0;
            const int gpu_export_requested =
                isolated_analysis ? 0 : llrawproc_gpu_export_enabled();
            const int gpu_export_trusted_requested =
                gpu_export_requested && llrawproc_gpu_export_trusted_enabled();
            const int gpu_playback_output_validation_requested =
                llrawproc_gpu_playback_recon_output_validation_enabled();
            int gpu_playback_recon_used = 0;
            int gpu_export_trusted_attempted = 0;
            int gpu_export_trusted_used = 0;
            int dual_iso_recon_ok = 0;
            int capture_gpu_recon_state = 0;
            int gpu_playback_no_readback_post_recon_fix_ran = 0;
            int gpu_playback_prepare_only_used = 0;
            int explicit_auto_correction = 0;
            double explicit_ev_correction = worker->diso_ev_correction;
            int explicit_black_delta = worker->diso_black_delta;
            const int has_explicit_auto_match =
                (worker->diso_auto_correction < 0) &&
                (worker->diso_ev_correction != 1) &&
                (worker->diso_black_delta != -1);

            int * auto_correction_ptr = has_explicit_auto_match
                ? &explicit_auto_correction
                : &worker->diso_auto_correction;
            double * ev_correction_ptr = has_explicit_auto_match
                ? &explicit_ev_correction
                : &worker->diso_ev_correction;
            int * black_delta_ptr = has_explicit_auto_match
                ? &explicit_black_delta
                : &worker->diso_black_delta;

            publish_auto_correction = !has_explicit_auto_match;

            if (gpu_export_requested)
            {
                if (raw_image_size == 0)
                {
                    llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_EMPTY_RAW_IMAGE);
                }
                else if (!llrawproc_gpu_recon_backend_available_guarded(0))
                {
                    llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_BACKEND_UNAVAILABLE);
                }
                else
                {
                    gpu_export_input = (uint16_t *)malloc(raw_image_size);
                    if (gpu_export_input)
                    {
                        memcpy(gpu_export_input, raw_image_buff, raw_image_size);
                        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_MISSING_RECON_STATE);
                    }
                    else
                    {
                        llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_INPUT_ALLOC_FAILED);
                    }
                }
            }
            if (!isolated_analysis
             && llrawproc_gpu_playback_recon_enabled()
             && raw_image_size > 0)
            {
                llrawproc_gpu_playback_reset_last_run_state();
                if (g_llrawproc_gpu_playback_texture_prepare_only_allowed
                 && g_llrawproc_gpu_playback_texture_present_preferred
                 && !gpu_playback_output_validation_requested
                 && !gpu_export_input)
                {
                    gpu_playback_input = raw_image_buff;
                    gpu_playback_input_borrowed_from_raw_image = 1;
                }
                else
                {
                    gpu_playback_input = (uint16_t *)malloc(raw_image_size);
                }
                if (gpu_playback_input)
                {
                    if (!gpu_playback_input_borrowed_from_raw_image)
                    {
                        memcpy(gpu_playback_input, raw_image_buff, raw_image_size);
                    }
                    /* No-readback eligibility (effectiveness-based, fail-closed):
                     * the CUDA->GL R16 texture the no-readback path presents is
                     * the RECON-ONLY Dual ISO bayer. The post-recon focus-pixel /
                     * bad-pixel interpolation (below, ~2509-2553) mutates
                     * raw_image_buff IN PLACE after diso_get_full20bit, and the
                     * CUDA backend has no focus/bad-pixel code. Store the input
                     * bayer now; if that interpolation ACTUALLY runs for this frame
                     * (map ready and applied), the stored input is RETRACTED right
                     * after that block so the Qt layer falls back to the
                     * CPU-readback bayer (which includes the fixes). Gating on the
                     * actual fix-effect -- NOT the focus_pixels/bad_pixels mode
                     * flags (bad_pixels defaults to 1) -- lets the no-readback path
                     * engage for the common case where the fix is a no-op, while
                     * staying fail-closed when it would change pixels. NOTE:
                     * vertical_stripes is PRE-recon and pattern_noise does not run
                     * for dual-iso frames, so they need no gate here. */
                    if (g_llrawproc_gpu_playback_texture_present_preferred
                     && !gpu_playback_input_borrowed_from_raw_image)
                    {
                        gpu_playback_input_owned_by_last_snapshot =
                            llrawproc_gpu_playback_take_last_input_bayer16(
                                gpu_playback_input,
                                raw_image_size / sizeof(uint16_t));
                    }
                }
            }

            if (gpu_playback_input)
            {
                dualiso_gpu_recon_state_t gpu_playback_state;
                memset(&gpu_playback_state, 0, sizeof(gpu_playback_state));
                /* Prepare may resolve Dual ISO pattern/match fields; CPU fallback
                 * intentionally reuses those idempotent resolved values below. */
                if (diso_prepare_gpu_recon_state(raw_info,
                                                 gpu_playback_input,
                                                 dark_frame_mode,
                                                 diso1,
                                                 diso2,
                                                 &worker->diso_pattern,
                                                 auto_correction_ptr,
                                                 ev_correction_ptr,
                                                 black_delta_ptr,
                                                 diso_averaging,
                                                 diso_alias_map,
                                                 diso_frblending,
                                                 chroma_smooth_mode,
                                                 isolated_analysis
                                                     ? 0
                                                     : processingPlaybackPreviewModeEnabled()
                                                     ? ((video && video->playback_scale_factor_active > 0)
                                                         ? video->playback_scale_factor_active
                                                         : processingPlaybackPreviewScaleFactor())
                                                     : 0,
                                                 llrawproc_threads,
                                                 &worker->diso_full20bit_scratch,
                                                 &gpu_playback_state))
                {
                    g_llrawproc_gpu_playback_last_state_valid =
                        gpu_playback_state.valid ? 1 : 0;
                    g_llrawproc_gpu_playback_last_prepared_state =
                        gpu_playback_state;
                }
                {
                    const int gpu_playback_post_recon_fix_would_run =
                        (focus_pixels && focus_interpolate_outside_lock
                         && focus_status_snapshot == 2 && focus_map_for_interpolation)
                     || (bad_pixels && bad_interpolate_outside_lock
                         && bad_status_snapshot == 2 && bad_map_for_interpolation);
                    const int gpu_playback_prepare_only_allowed =
                        g_llrawproc_gpu_playback_texture_prepare_only_allowed
                        && g_llrawproc_gpu_playback_texture_present_preferred
                        && !gpu_playback_output_validation_requested
                        && !gpu_export_input
                        && gpu_playback_state.valid
                        && llrawproc_gpu_playback_recon_state_matches_validated_config(
                            &gpu_playback_state)
                        && !gpu_playback_post_recon_fix_would_run;
                    if (gpu_playback_prepare_only_allowed)
                    {
                        if (llrawproc_gpu_playback_retain_device_output_enabled())
                        {
                            llrpGpuPlaybackReconState_t public_gpu_playback_state;
                            llrpGpuPlaybackRetainedDeviceBayer16_t retained_device;
                            int retained_rc = -1;
                            memset(&public_gpu_playback_state,
                                   0,
                                   sizeof(public_gpu_playback_state));
                            memset(&retained_device, 0, sizeof(retained_device));
                            llrawproc_gpu_playback_public_state_from_dualiso(
                                &gpu_playback_state,
                                &public_gpu_playback_state);
                            /* Same-thread, synchronous call (this runs inside
                             * applyLLRawProcObjectWorker on the render worker
                             * thread that just set the current frame index),
                             * so this already-armed accessor names the right
                             * frame -- see the note on the run_cpu16 call in
                             * llrawproc_gpu_playback_try_reconstruct() above. */
                            public_gpu_playback_state.frame_id =
                                mlv_pipeline_capture_get_current_frame();
                            g_llrawproc_gpu_playback_last_run_attempted = 1;
                            if (llrpGpuPlaybackReconRunRetainedDeviceBayer16(
                                    &public_gpu_playback_state,
                                    gpu_playback_input,
                                    raw_image_size,
                                    &retained_device,
                                    &retained_rc,
                                    NULL)
                             && retained_device.valid)
                            {
                                g_llrawproc_gpu_playback_last_retained_device_bayer16 =
                                    retained_device;
                                g_llrawproc_gpu_playback_last_run_rc = retained_rc;
                                g_llrawproc_gpu_playback_last_used = 1;
                            }
                            else
                            {
                                memset(
                                    &g_llrawproc_gpu_playback_last_retained_device_bayer16,
                                    0,
                                    sizeof(g_llrawproc_gpu_playback_last_retained_device_bayer16));
                                g_llrawproc_gpu_playback_last_run_rc = retained_rc;
                            }
                        }
                        dual_iso_recon_ok = 1;
                        gpu_playback_prepare_only_used = 1;
                        g_llrawproc_gpu_playback_last_prepare_only = 1;
                        if (!llrawproc_gpu_playback_retain_device_output_enabled()
                         || g_llrawproc_gpu_playback_last_retained_device_bayer16.valid)
                        {
                            g_llrawproc_gpu_playback_last_run_rc = 0;
                        }
                    }
                    else if (gpu_playback_state.valid
                     && !g_llrawproc_gpu_playback_texture_present_preferred
                     && llrawproc_gpu_playback_try_reconstruct(&gpu_playback_state,
                                                               gpu_playback_input,
                                                               raw_image_buff,
                                                               raw_image_size))
                    {
                        dual_iso_recon_ok = 1;
                        gpu_playback_recon_used = 1;
                    }
                }
            }

            if (!gpu_playback_recon_used
             && gpu_export_trusted_requested
             && gpu_export_input)
            {
                dualiso_gpu_recon_state_t gpu_export_state;
                memset(&gpu_export_state, 0, sizeof(gpu_export_state));
                if (diso_prepare_gpu_recon_state(raw_info,
                                                 gpu_export_input,
                                                 dark_frame_mode,
                                                 diso1,
                                                 diso2,
                                                 &worker->diso_pattern,
                                                 auto_correction_ptr,
                                                 ev_correction_ptr,
                                                 black_delta_ptr,
                                                 diso_averaging,
                                                 diso_alias_map,
                                                 diso_frblending,
                                                 chroma_smooth_mode,
                                                 isolated_analysis
                                                     ? 0
                                                     : processingPlaybackPreviewModeEnabled()
                                                     ? ((video && video->playback_scale_factor_active > 0)
                                                         ? video->playback_scale_factor_active
                                                         : processingPlaybackPreviewScaleFactor())
                                                     : 0,
                                                 llrawproc_threads,
                                                 &worker->diso_full20bit_scratch,
                                                 &gpu_export_state))
                {
                    gpu_export_trusted_used =
                        llrawproc_gpu_export_try_trusted(&gpu_export_state,
                                                         gpu_export_input,
                                                         raw_image_buff,
                                                         raw_image_size);
                    gpu_export_trusted_attempted =
                        g_llrawproc_gpu_export_last_run_attempted;
                    if (gpu_export_trusted_used)
                    {
                        dual_iso_recon_ok = 1;
                    }
                }
                else
                {
                    llrawproc_gpu_export_set_skip_code(
                        LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE);
                }
            }

            if (!gpu_playback_recon_used
             && !gpu_export_trusted_used
             && !gpu_playback_prepare_only_used)
            {
                capture_gpu_recon_state =
                    (gpu_export_input != NULL && !gpu_export_trusted_requested)
                    || (gpu_playback_input != NULL
                     && g_llrawproc_gpu_playback_texture_present_preferred);
                dualiso_debug_set_gpu_recon_state_capture_enabled(capture_gpu_recon_state);
                dual_iso_recon_ok =
                    diso_get_full20bit(raw_info,
                                       raw_image_buff,
                                       dark_frame_mode,
                                       diso1,
                                       diso2,
                                       &worker->diso_pattern,
                                       auto_correction_ptr,
                                       ev_correction_ptr,
                                       black_delta_ptr,
                                       diso_averaging,
                                       diso_alias_map,
                                       diso_frblending,
                                       chroma_smooth_mode,
                                       isolated_analysis
                                           ? 0
                                           : processingPlaybackPreviewModeEnabled()
                                           ? ((video && video->playback_scale_factor_active > 0)
                                               ? video->playback_scale_factor_active
                                               : processingPlaybackPreviewScaleFactor())
                                           : 0,
                                       llrawproc_threads,
                                       &worker->diso_full20bit_scratch);
            }
            /* P3 diagnostic (gated, one-shot): snapshot the RECON-ONLY bayer here --
             * raw_image_buff is the diso_get_full20bit output, BEFORE the post-recon
             * raw-fix stages (focus pixels, etc.). The live-vector dump's out.u16 is
             * captured AFTER the full pipeline, so dumping recon_only.u16 here lets the
             * offline triad isolate a recon state-capture desync (recon_only vs
             * backend_cpu) from post-recon stages (recon_only vs out.u16). */
            if (!gpu_playback_recon_used
             && !gpu_playback_prepare_only_used
             && dual_iso_recon_ok
             && g_llrawproc_gpu_playback_texture_present_preferred)
            {
                static int recon_only_dumped = 0;
                const char * dump_dir =
                    getenv("MLVAPP_GPU_PLAYBACK_RECON_DUMP_LIVE_VECTOR_DIR");
                if (!recon_only_dumped && dump_dir && *dump_dir
                 && strcmp(dump_dir, "0") != 0 && raw_image_buff && raw_image_size > 0)
                {
                    char recon_only_path[1200];
                    snprintf(recon_only_path, sizeof(recon_only_path),
                             "%s/recon_only.u16", dump_dir);
                    FILE * recon_only_f = fopen(recon_only_path, "wb");
                    if (recon_only_f)
                    {
                        fwrite(raw_image_buff, 1, raw_image_size, recon_only_f);
                        fclose(recon_only_f);
                        recon_only_dumped = 1;
                    }
                }
            }
            if (!gpu_playback_recon_used
             && !gpu_playback_prepare_only_used
             && dual_iso_recon_ok
             && gpu_playback_input
             && g_llrawproc_gpu_playback_texture_present_preferred
             && capture_gpu_recon_state)
            {
                dualiso_gpu_recon_state_t cpu_oracle_state;
                memset(&cpu_oracle_state, 0, sizeof(cpu_oracle_state));
                if (dualiso_debug_get_last_gpu_recon_state(&cpu_oracle_state)
                 && cpu_oracle_state.valid)
                {
                    g_llrawproc_gpu_playback_last_state_valid = 1;
                    g_llrawproc_gpu_playback_last_prepared_state = cpu_oracle_state;
                }
            }
            if (gpu_export_input)
            {
                if (gpu_export_trusted_used)
                {
                    /* Trusted mode already wrote the GPU recon into raw_image_buff. */
                }
                else if (gpu_export_trusted_requested)
                {
                    if (!gpu_export_trusted_attempted)
                    {
                        llrawproc_gpu_export_set_skip_code(
                            LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE);
                    }
                }
                else if (gpu_playback_recon_used)
                {
                    llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_PLAYBACK_RECON_USED);
                }
                else if (!dual_iso_recon_ok)
                {
                    llrawproc_gpu_export_set_skip_code(LLRP_GPU_EXPORT_SKIP_DUAL_ISO_RECON_FAILED);
                }
                else
                {
                    llrawproc_gpu_export_try_replace(raw_image_buff,
                                                     gpu_export_input,
                                                     raw_image_size);
                }
                free(gpu_export_input);
            }
            if (gpu_playback_input)
            {
                if (!gpu_playback_input_owned_by_last_snapshot
                 && !gpu_playback_input_borrowed_from_raw_image)
                {
                    free(gpu_playback_input);
                }
            }
            dualiso_debug_set_gpu_recon_state_capture_enabled(0);
            dualiso_debug_get_full20bit_timing(
                &g_llrawproc_last_dual_iso_full20bit_timing);
            dual_iso_ms += (mlv_stage_timing_now() - dual_iso_start) * 1000.0;

            if (!dual_iso_recon_ok)
            {
                if (dual_iso_failure_backup)
                {
                    memcpy(raw_image_buff, dual_iso_failure_backup, raw_image_size);
                }
                if (original_bits_per_pixel < 14)
                {
                    undo_14bit(raw_image_buff,
                               raw_image_size,
                               original_bits_per_pixel);
                }
                llrawproc_restore_worker_runtime_state(
                    worker,
                    &worker->seeded_runtime_state);
            }
            else
            {
                if (has_explicit_auto_match)
                {
                    worker->diso_ev_correction = explicit_ev_correction;
                    worker->diso_black_delta = explicit_black_delta;
                }

                {
                    int bits_shift = 16 - raw_info.bits_per_pixel;
                    worker->dng_black_level = raw_info.black_level << bits_shift;
                    worker->dng_white_level = raw_info.white_level << bits_shift;
                    worker->dng_bit_depth = 16;
                }

            if (gpu_playback_prepare_only_used)
            {
                /* The CPU recon/display buffer is intentionally not produced in
                 * this admitted no-readback mode; skip post-recon CPU-only raw
                 * fixes after proving above that they would not run for this
                 * frame. If they would run, prepare-only is not admitted and the
                 * existing CPU/readback path remains the fallback. */
                focus_pixels = 0;
                bad_pixels = 0;
            }

            const double refine_lock_start = mlv_stage_timing_now();
            int post_recon_luts_active = 0;
            focus_status_snapshot = 0;
            bad_status_snapshot = 0;
            focus_interpolate_outside_lock = 0;
            bad_interpolate_outside_lock = 0;
            bad_force_reset_after_interpolation = 0;
            focus_map_for_interpolation = NULL;
            bad_map_for_interpolation = NULL;
            if (bad_pixels)
            {
                llrawproc_worker_ensure_luts(worker, worker->dng_black_level);
                post_recon_luts_active = 1;
            }
            pthread_mutex_lock(&video->llrawproc_mutex);
            if (focus_pixels)
            {
                if (shared->fpm_status < 2)
                {
                    int crop_rec = crop_rec_mode ? 1 : (focus_pixels == 2);
                    prepare_focus_pixel_map(&shared->focus_pixel_map,
                                            &shared->fpm_status,
                                            camera_id,
                                            raw_width,
                                            raw_height,
                                            crop_rec,
                                            unified_mode);
                    llrawproc_bump_focus_map_version(shared);
                }
                focus_status_snapshot = shared->fpm_status;
                if (focus_status_snapshot == 2)
                {
                    focus_map_for_interpolation = llrawproc_worker_get_focus_map_copy(worker, shared);
                    focus_interpolate_outside_lock = (focus_map_for_interpolation != NULL);
                }
                else focus_interpolate_outside_lock = 0;
            }

            if (bad_pixels)
            {
                if (shared->bpm_status < 2 || (shared->bpm_status == 2 && bad_pixels == 2))
                {
                    bad_force_reset_after_interpolation = prepare_bad_pixel_map(&shared->bad_pixel_map,
                                                                                &shared->bpm_status,
                                                                                raw_image_buff,
                                                                                camera_id,
                                                                                x_res,
                                                                                y_res,
                                                                                pan_pos_x,
                                                                                pan_pos_y,
                                                                                raw_width,
                                                                                raw_height,
                                                                                raw_info.black_level,
                                                                                bad_pixels,
                                                                                bps_method,
                                                                                worker->raw2ev);
                    llrawproc_bump_bad_map_version(shared);
                }
                bad_status_snapshot = shared->bpm_status;
                if (bad_status_snapshot == 2)
                {
                    bad_map_for_interpolation = llrawproc_worker_get_bad_map_copy(worker, shared);
                    bad_interpolate_outside_lock = (bad_map_for_interpolation != NULL);
                }
                else bad_interpolate_outside_lock = 0;
            }
            pthread_mutex_unlock(&video->llrawproc_mutex);
            g_llrawproc_last_dualiso_refine_lock_ms += (mlv_stage_timing_now() - refine_lock_start) * 1000.0;
            g_llrawproc_last_shared_lock_ms += g_llrawproc_last_dualiso_refine_lock_ms;

            if (focus_pixels && focus_interpolate_outside_lock && focus_status_snapshot == 2 && focus_map_for_interpolation)
            {
                if (!post_recon_luts_active)
                {
                    llrawproc_worker_ensure_luts(worker, worker->dng_black_level);
                    post_recon_luts_active = 1;
                }
                const double focus_pixels_start = mlv_stage_timing_now();
                interpolate_focus_pixel_map(focus_map_for_interpolation,
                                            raw_image_buff,
                                            x_res,
                                            y_res,
                                            pan_pos_x,
                                            pan_pos_y,
                                            2,
                                            0,
                                            worker->raw2ev,
                                            worker->ev2raw);
                focus_pixels_ms += (mlv_stage_timing_now() - focus_pixels_start) * 1000.0;
            }

            if (bad_pixels && bad_interpolate_outside_lock && bad_status_snapshot == 2 && bad_map_for_interpolation)
            {
                if (!post_recon_luts_active)
                {
                    llrawproc_worker_ensure_luts(worker, worker->dng_black_level);
                    post_recon_luts_active = 1;
                }
                const double bad_pixels_start = mlv_stage_timing_now();
                interpolate_bad_pixel_map(bad_map_for_interpolation,
                                          raw_image_buff,
                                          x_res,
                                          y_res,
                                          pan_pos_x,
                                          pan_pos_y,
                                          2,
                                          0,
                                          worker->raw2ev,
                                          worker->ev2raw);
                if (bad_force_reset_after_interpolation)
                {
                    llrawproc_reset_force_bad_pixel_search(video, bad_pixels);
                }
                bad_pixels_ms += (mlv_stage_timing_now() - bad_pixels_start) * 1000.0;
            }

            /* No-readback retract: if the post-recon focus/bad-pixel interpolation
             * actually ran for this frame (mirrors the exact apply conditions at
             * ~2508 / ~2529 above), the recon-only no-readback texture would differ
             * from the CPU display frame. Drop the stored no-readback input so the
             * Qt layer falls back to the CPU-readback bayer (which has the fixes).
             * These predicates are unchanged since the interpolation ran. */
            gpu_playback_no_readback_post_recon_fix_ran =
                (focus_pixels && focus_interpolate_outside_lock
                 && focus_status_snapshot == 2 && focus_map_for_interpolation)
             || (bad_pixels && bad_interpolate_outside_lock
                 && bad_status_snapshot == 2 && bad_map_for_interpolation);
            if (gpu_playback_no_readback_post_recon_fix_ran
             && g_llrawproc_gpu_playback_texture_present_preferred)
            {
                if (g_llrawproc_gpu_playback_last_input_bayer16)
                {
                    free(g_llrawproc_gpu_playback_last_input_bayer16);
                    g_llrawproc_gpu_playback_last_input_bayer16 = NULL;
                }
                g_llrawproc_gpu_playback_last_input_words = 0;
            }

            if (post_recon_luts_active)
            {
                llrawproc_worker_ensure_luts(worker, raw_info.black_level);
            }
            }
            dual_iso_failure_backup = NULL;
        }
        else if (dual_iso_mode == 2)
        {
            const double dual_iso_start = mlv_stage_timing_now();
            diso_get_preview(raw_image_buff,
                             raw_info.width,
                             raw_info.height,
                             raw_info.black_level,
                             raw_info.white_level,
                             &worker->diso_pattern,
                             0,
                             &worker->diso_preview_scratch);
            dual_iso_ms += (mlv_stage_timing_now() - dual_iso_start) * 1000.0;
            g_llrawproc_last_preview_histogram_ms = worker->diso_preview_scratch.last_histogram_ms;
            g_llrawproc_last_preview_regression_ms = worker->diso_preview_scratch.last_regression_ms;
            g_llrawproc_last_preview_rowscale_ms = worker->diso_preview_scratch.last_rowscale_ms;
        }
    }

    /* S2_post_dualiso capture: post Dual ISO recon (full HQ or preview
     * rowscale), before chroma smooth. If dual_iso_mode==0 the capture
     * happens with no transformation since the if-block above was
     * skipped — useful as a "this clip has no Dual ISO" baseline. */
    {
        const uint64_t frame_index = mlv_pipeline_capture_get_current_frame();
        if (mlv_pipeline_capture_should_capture_frame(frame_index))
        {
            mlv_pipeline_capture_meta_t meta;
            memset(&meta, 0, sizeof meta);
            meta.stage = MLV_PIPELINE_STAGE_S2_POST_DUALISO;
            meta.format = MLV_PIPELINE_FORMAT_UINT16_MONO;
            meta.format_label = "uint16_bayer_post_dualiso";
            meta.width = x_res;
            meta.height = y_res;
            meta.bytes_per_line = x_res * (int)sizeof(uint16_t);
            meta.bytes_per_pixel = (int)sizeof(uint16_t);
            meta.channels = 1;
            meta.bit_depth = 16;
            meta.dual_iso_mode = (dual_iso_mode == 0) ? "off"
                               : (dual_iso_mode == 1) ? "full"
                               : (dual_iso_mode == 2) ? "preview"
                               : "unknown";
            meta.debayer_mode = "n/a";
            meta.scaler = "none";
            meta.path_label = "applyLLRawProcObject_post_dualiso";
            mlv_pipeline_capture(frame_index, raw_image_buff, &meta);
        }
    }

    if (chroma_smooth_mode && dual_iso_mode != 1)
    {
        const double chroma_smooth_start = mlv_stage_timing_now();
#ifndef STDOUT_SILENT
        printf("\nUsing chroma smooth method: '%dx%d'\n\n", chroma_smooth_mode, chroma_smooth_mode);
#endif
        chroma_smooth(chroma_smooth_mode,
                      raw_image_buff,
                      x_res,
                      y_res,
                      raw_info.black_level,
                      raw_info.white_level,
                      worker->raw2ev,
                      worker->ev2raw,
                      &worker->chroma_smooth_scratch);
        chroma_smooth_ms += (mlv_stage_timing_now() - chroma_smooth_start) * 1000.0;
    }

    if (original_bits_per_pixel < 14 && dual_iso_mode != 1)
    {
        undo_14bit(raw_image_buff, raw_image_size, video->RAWI.raw_info.bits_per_pixel);
    }

    {
        const llrawproc_runtime_state_t runtime_state = llrawproc_capture_worker_runtime_state(worker);
        const int runtime_state_changed =
            !llrawproc_runtime_state_equal(&runtime_state,
                                           &worker->seeded_runtime_state,
                                           publish_auto_correction);
        if (!isolated_analysis && runtime_state_changed)
        {
            const double publish_lock_start = mlv_stage_timing_now();
            pthread_mutex_lock(&video->llrawproc_mutex);
            llrawproc_publish_worker_results(video, &runtime_state, publish_auto_correction);
            pthread_mutex_unlock(&video->llrawproc_mutex);
            g_llrawproc_last_publish_lock_ms += (mlv_stage_timing_now() - publish_lock_start) * 1000.0;
            g_llrawproc_last_shared_lock_ms += g_llrawproc_last_publish_lock_ms;
        }
    }

    /* deflicker RAW data by changing 'tcBaselineExposure' tag in the exported DNG */
    /*
    if (video->llrawproc->deflicker_target)
    {
#ifndef STDOUT_SILENT
        printf("Per-frame exposure compensation: 'ON'\nDeflicker target: '%d'\n\n", video->llrawproc->deflicker_target);
#endif
        deflicker(video, raw_image_buff, raw_image_size);
    }
    */

#ifndef STDOUT_SILENT
    if (video->processing
     && raw_image_buff
     && raw_image_size / sizeof(*raw_image_buff) > 1000)
    {
        printf("raw_image_buff[1000] = %u, Proc_Black = %f, Proc_White = %d, Raw_Black = %d, Raw_White = %d <= THE END OF LLRAWPROC\n",
               (unsigned int)raw_image_buff[1000],
               (double)video->processing->black_level,
               video->processing->white_level,
               video->RAWI.raw_info.black_level,
               video->RAWI.raw_info.white_level);
    }
#endif

    g_llrawproc_last_dark_frame_ms = dark_frame_ms;
    g_llrawproc_last_vertical_stripes_ms = vertical_stripes_ms;
    g_llrawproc_last_focus_pixels_ms = focus_pixels_ms;
    g_llrawproc_last_bad_pixels_ms = bad_pixels_ms;
    g_llrawproc_last_pattern_noise_ms = pattern_noise_ms;
    g_llrawproc_last_dual_iso_ms = dual_iso_ms;
    g_llrawproc_last_chroma_smooth_ms = chroma_smooth_ms;
    g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;

    if (using_stack_worker)
    {
        llrawproc_free_worker_state(worker);
    }
}

static void applyLLRawProcObjectWorkerIsolatedAnalysisInternal(
    mlvObject_t * video,
    uint16_t * raw_image_buff,
    size_t raw_image_size,
    llrawprocWorkerState_t * worker,
    int stop_before_dual_iso,
    int chroma_smooth_override_enabled,
    int chroma_smooth_method)
{
    const int previous_isolation = g_llrawproc_analysis_isolation_enabled;
    const int previous_override_enabled =
        g_llrawproc_analysis_chroma_smooth_override_enabled;
    const int previous_override = g_llrawproc_analysis_chroma_smooth_override;
#ifdef _OPENMP
    const int previous_omp_threads = omp_get_max_threads();
    omp_set_num_threads(1);
#endif
    g_llrawproc_analysis_isolation_enabled = 1;
    g_llrawproc_analysis_chroma_smooth_override_enabled =
        chroma_smooth_override_enabled ? 1 : 0;
    g_llrawproc_analysis_chroma_smooth_override =
        llrawproc_normalize_chroma_smooth_method(chroma_smooth_method);
    applyLLRawProcObjectWorker(video,
                               raw_image_buff,
                               raw_image_size,
                               worker,
                               stop_before_dual_iso);
    g_llrawproc_analysis_chroma_smooth_override_enabled =
        previous_override_enabled;
    g_llrawproc_analysis_chroma_smooth_override = previous_override;
    g_llrawproc_analysis_isolation_enabled = previous_isolation;
#ifdef _OPENMP
    omp_set_num_threads(previous_omp_threads);
#endif
}

void applyLLRawProcObjectWorkerIsolatedAnalysis(mlvObject_t * video,
                                                uint16_t * raw_image_buff,
                                                size_t raw_image_size,
                                                llrawprocWorkerState_t * worker,
                                                int stop_before_dual_iso)
{
    applyLLRawProcObjectWorkerIsolatedAnalysisInternal(video,
                                                       raw_image_buff,
                                                       raw_image_size,
                                                       worker,
                                                       stop_before_dual_iso,
                                                       0,
                                                       CS_OFF);
}

void applyLLRawProcObjectWorkerIsolatedAnalysisWithChromaSmooth(mlvObject_t * video,
                                                                uint16_t * raw_image_buff,
                                                                size_t raw_image_size,
                                                                llrawprocWorkerState_t * worker,
                                                                int stop_before_dual_iso,
                                                                int chroma_smooth_method)
{
    applyLLRawProcObjectWorkerIsolatedAnalysisInternal(video,
                                                       raw_image_buff,
                                                       raw_image_size,
                                                       worker,
                                                       stop_before_dual_iso,
                                                       1,
                                                       chroma_smooth_method);
}

void applyLLRawProcObject(mlvObject_t * video, uint16_t * raw_image_buff, size_t raw_image_size)
{
    applyLLRawProcObjectWorker(video, raw_image_buff, raw_image_size, NULL, 0);
}

void applyLLRawProcObjectPreDualIsoFixes(mlvObject_t * video,
                                         uint16_t * raw_image_buff,
                                         size_t raw_image_size)
{
    applyLLRawProcObjectWorker(video, raw_image_buff, raw_image_size, NULL, 1);
}

/* Phase 4B-v2: scaled-buffer entry point. Runs a SUBSET of the llrawproc
 * pipeline on a buffer whose dimensions differ from
 * video->RAWI.xRes/yRes. The subset includes only the size-agnostic
 * stages (HQ Dual ISO recon, dark frame subtraction, chroma smooth, 14-bit
 * conversion). Pre-downsample stages (focus pixel, bad pixel, vertical
 * stripes, pattern noise) are NOT applied here — the caller must apply
 * them at full res before downsampling, OR ensure they are disabled in
 * the receipt. Aggressive preview is the one deliberate exception: it may
 * skip those coordinate-sensitive stages to keep reduction ahead of Dual
 * ISO/debayer for coarse deep preview.
 *
 * Returns 1 if the scaled application is safe (caller can proceed), 0 if
 * a feature in the receipt is incompatible with the scaled path (caller
 * must fall back to the v1 full-res path).
 *
 * Threading: this function is callable from playback worker threads. It
 * shares the per-clip worker state with applyLLRawProcObject (acquires the
 * worker via llrawproc_acquire_worker_state). The shared->diso_pattern
 * field is read but not written from the scaled path — so the iso pattern
 * detection MUST have been seeded by a prior full-res render. */
int applyLLRawProcObject_with_dims(mlvObject_t * video,
                                   uint16_t * raw_image_buff,
                                   size_t raw_image_size,
                                   int override_w,
                                   int override_h)
{
    const double apply_start = mlv_stage_timing_now();
    llrawprocObject_t * shared = video ? video->llrawproc : NULL;
    const int isolated_analysis = g_llrawproc_analysis_isolation_enabled;
    const int llrawproc_threads =
        llrawproc_analysis_thread_count(video, isolated_analysis);
    llrawprocWorkerState_t stack_worker;
    llrawprocWorkerState_t * worker = NULL;
    int using_stack_worker = 0;

    g_llrawproc_last_shared_lock_ms = 0.0;
    g_llrawproc_last_dualiso_refine_lock_ms = 0.0;
    g_llrawproc_last_publish_lock_ms = 0.0;
    g_llrawproc_last_total_ms = 0.0;
    g_llrawproc_last_dark_frame_ms = 0.0;
    g_llrawproc_last_vertical_stripes_ms = 0.0;
    g_llrawproc_last_focus_pixels_ms = 0.0;
    g_llrawproc_last_bad_pixels_ms = 0.0;
    g_llrawproc_last_pattern_noise_ms = 0.0;
    g_llrawproc_last_dual_iso_ms = 0.0;
    g_llrawproc_last_chroma_smooth_ms = 0.0;
    llrawproc_reset_dual_iso_full20bit_timing();
    g_llrawproc_last_preview_histogram_ms = 0.0;
    g_llrawproc_last_preview_regression_ms = 0.0;
    g_llrawproc_last_preview_rowscale_ms = 0.0;

    if (!video || !shared || !shared->fix_raw)
    {
        g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
        return 1; /* nothing to do — no recon, no fix */
    }
    size_t override_pixel_count = 0;
    uint32_t override_frame_size = 0;
    if (!llrawproc_checked_14bit_frame_extents(override_w,
                                              override_h,
                                              &override_pixel_count,
                                              &override_frame_size)
        || override_pixel_count > SIZE_MAX / sizeof(uint16_t)
        || override_pixel_count * sizeof(uint16_t) != raw_image_size)
    {
        return 0;
    }

    /* Bail if the receipt enables features that are unsafe at scaled
     * resolution. Aggressive preview explicitly treats those stages as
     * skippable approximations; sharp/smooth playback falls back to the
     * conservative full-res path. */
    if (!mlvPlaybackAggressivePreviewMode())
    {
        if (shared->focus_pixels) return 0;
        if (shared->bad_pixels) return 0;
        if (shared->vertical_stripes) return 0;
        if (shared->pattern_noise) return 0;
    }

    memset(&stack_worker, 0, sizeof(stack_worker));
    stack_worker.prev_black_level = -1;
    worker = llrawproc_acquire_worker_state(video);
    if (!worker)
    {
        worker = &stack_worker;
        using_stack_worker = 1;
    }

    /* Build a local raw_info with the override dimensions. The dual ISO
     * recon reads raw_info.width/height/pitch + active_area. */
    struct raw_info raw_info = video->RAWI.raw_info;
    const int original_bits_per_pixel = video->RAWI.raw_info.bits_per_pixel;

    raw_info.width = override_w;
    raw_info.height = override_h;
    /* The Dual ISO core receives uint16_t samples and defines pitch in
     * samples, matching the full-resolution caller. */
    raw_info.pitch = override_w;
    raw_info.frame_size = override_frame_size;
    raw_info.active_area.x1 = 0;
    raw_info.active_area.y1 = 0;
    raw_info.active_area.x2 = override_w;
    raw_info.active_area.y2 = override_h;

    int diso_validity = 0;
    int dual_iso_mode = 0;
    int diso1 = 0;
    int diso2 = 0;
    int diso_averaging = 0;
    int diso_alias_map = 0;
    int diso_frblending = 0;
    int chroma_smooth_mode = 0;
    int dark_frame_mode = 0;
    int worker_diso_pattern = 0;
    int worker_diso_auto_correction = 0;
    double worker_diso_ev_correction = 0.0;
    int worker_diso_black_delta = 0;
    int apply_dark_frame_outside_lock = 0;
    const uint16_t * dark_frame_data_for_subtraction = NULL;
    uint32_t dark_frame_size_for_subtraction = 0;
    uint32_t dark_frame_black_level = 0;

    if (original_bits_per_pixel < 14)
    {
        if (!make_14bit(raw_image_buff, raw_image_size, &raw_info))
        {
            if (using_stack_worker) llrawproc_free_worker_state(worker);
            return 0;
        }
    }

    llrawproc_worker_reset_dng_bw_levels(worker, &raw_info);
    llrawproc_worker_ensure_luts(worker, raw_info.black_level);

    double shared_lock_start = mlv_stage_timing_now();
    pthread_mutex_lock(&video->llrawproc_mutex);
    if (!shared->fix_raw)
    {
        pthread_mutex_unlock(&video->llrawproc_mutex);
        g_llrawproc_last_shared_lock_ms = (mlv_stage_timing_now() - shared_lock_start) * 1000.0;
        g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
        if (using_stack_worker) llrawproc_free_worker_state(worker);
        return 1;
    }

    /* Dark frame: only safe if its dimensions match the override. */
    if (!df_init(video))
    {
        if (llrawproc_worker_sync_dark_frame_copy(worker, shared)
         && worker->dark_frame_data_copy
         && worker->dark_frame_size == raw_image_size)
        {
            apply_dark_frame_outside_lock = 1;
            dark_frame_data_for_subtraction = worker->dark_frame_data_copy;
            dark_frame_size_for_subtraction = worker->dark_frame_size;
            dark_frame_black_level = worker->dark_frame_hdr_copy.black_level;
        }
        /* If the dark frame size doesn't match the scaled buffer, we
         * can't apply it at scale — bail and let the caller fall back. */
        else if (worker->dark_frame_size != 0)
        {
            pthread_mutex_unlock(&video->llrawproc_mutex);
            g_llrawproc_last_shared_lock_ms = (mlv_stage_timing_now() - shared_lock_start) * 1000.0;
            g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;
            if (using_stack_worker) llrawproc_free_worker_state(worker);
            return 0;
        }
    }

    diso_validity = shared->diso_validity;
    dual_iso_mode = shared->dual_iso;
    diso1 = shared->diso1;
    diso2 = shared->diso2;
    diso_averaging = shared->diso_averaging;
    if (!isolated_analysis
        && shared->diso_playback_force_mean23 != 0
        && !dualiso_playback_mean23_override_disabled_via_env())
    {
        diso_averaging = 1; /* DISOI_MEAN23 */
    }
    diso_alias_map = shared->diso_alias_map;
    diso_frblending = shared->diso_frblending;
    /* Phase E5 scale-aware downgrade: see note in applyLLRawProcObject. */
    if (!isolated_analysis
        && !dualiso_playback_alias_map_downgrade_disabled_via_env())
    {
        if (shared->diso_playback_force_disable_alias_map != 0)
        {
            diso_alias_map = 0;
        }
    }
    worker_diso_pattern = shared->diso_pattern;
    worker_diso_auto_correction = shared->diso_auto_correction;
    worker_diso_ev_correction = shared->diso_ev_correction;
    worker_diso_black_delta = shared->diso_black_delta;
    worker->seeded_runtime_state = llrawproc_capture_shared_runtime_state(shared);
    if (isolated_analysis)
    {
        llrawproc_runtime_state_t analysis_seed = { 0 };
        worker_diso_pattern = 0;
        worker_diso_auto_correction = -1;
        worker_diso_ev_correction = 1.0;
        worker_diso_black_delta = -1;
        analysis_seed.diso_auto_correction = -1;
        analysis_seed.diso_ev_correction = 1.0;
        analysis_seed.diso_black_delta = -1;
        worker->seeded_runtime_state = analysis_seed;
    }
    dark_frame_mode = shared->dark_frame;
    chroma_smooth_mode =
        llrawproc_analysis_chroma_smooth_method(shared->chroma_smooth,
                                                isolated_analysis);

    pthread_mutex_unlock(&video->llrawproc_mutex);
    g_llrawproc_last_shared_lock_ms += (mlv_stage_timing_now() - shared_lock_start) * 1000.0;

    worker->diso_pattern = worker_diso_pattern;
    worker->diso_auto_correction = worker_diso_auto_correction;
    worker->diso_ev_correction = worker_diso_ev_correction;
    worker->diso_black_delta = worker_diso_black_delta;

    if (apply_dark_frame_outside_lock)
    {
        const double dark_frame_start = mlv_stage_timing_now();
        df_subtract_snapshot(dark_frame_data_for_subtraction,
                             dark_frame_size_for_subtraction,
                             dark_frame_black_level,
                             raw_info.bits_per_pixel,
                             raw_image_buff,
                             raw_image_size);
        g_llrawproc_last_dark_frame_ms = (mlv_stage_timing_now() - dark_frame_start) * 1000.0;
    }

    int publish_auto_correction = 1;
    double dual_iso_ms = 0.0;
    double chroma_smooth_ms = 0.0;

    if (diso_validity && dual_iso_mode == 1)
    {
        int restricted_lossless = (video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) && raw_info.white_level < 15000;
        uint16_t * dual_iso_failure_backup = NULL;
        if (restricted_lossless)
        {
            dual_iso_failure_backup =
                llrawproc_worker_capture_dual_iso_failure_backup(
                    worker,
                    raw_image_buff,
                    raw_image_size);
            if (!dual_iso_failure_backup)
            {
                if (original_bits_per_pixel < 14)
                {
                    undo_14bit(raw_image_buff,
                               raw_image_size,
                               original_bits_per_pixel);
                }
                if (using_stack_worker) llrawproc_free_worker_state(worker);
                return 0;
            }
            const double dual_iso_start = mlv_stage_timing_now();
            int low_iso = MIN(diso1, diso2);
            int high_iso = MAX(diso1, diso2);
            if (!scale_restricted_range(&raw_info,
                                        raw_image_buff,
                                        override_pixel_count,
                                        low_iso,
                                        high_iso))
            {
                if (original_bits_per_pixel < 14)
                {
                    undo_14bit(raw_image_buff,
                               raw_image_size,
                               original_bits_per_pixel);
                }
                llrawproc_restore_worker_runtime_state(
                    worker,
                    &worker->seeded_runtime_state);
                if (using_stack_worker) llrawproc_free_worker_state(worker);
                return 0;
            }
            llrawproc_worker_reset_dng_bw_levels(worker, &raw_info);
            dual_iso_ms += (mlv_stage_timing_now() - dual_iso_start) * 1000.0;
        }

        const double dual_iso_start = mlv_stage_timing_now();
        int explicit_auto_correction = 0;
        double explicit_ev_correction = worker->diso_ev_correction;
        int explicit_black_delta = worker->diso_black_delta;
        const int has_explicit_auto_match =
            (worker->diso_auto_correction < 0) &&
            (worker->diso_ev_correction != 1) &&
            (worker->diso_black_delta != -1);

        int * auto_correction_ptr = has_explicit_auto_match
            ? &explicit_auto_correction
            : &worker->diso_auto_correction;
        double * ev_correction_ptr = has_explicit_auto_match
            ? &explicit_ev_correction
            : &worker->diso_ev_correction;
        int * black_delta_ptr = has_explicit_auto_match
            ? &explicit_black_delta
            : &worker->diso_black_delta;

        publish_auto_correction = !has_explicit_auto_match;

        const int dual_iso_recon_ok =
            diso_get_full20bit(raw_info,
                               raw_image_buff,
                               dark_frame_mode,
                               diso1,
                               diso2,
                               &worker->diso_pattern,
                               auto_correction_ptr,
                               ev_correction_ptr,
                               black_delta_ptr,
                               diso_averaging,
                               diso_alias_map,
                               diso_frblending,
                               chroma_smooth_mode,
                               isolated_analysis
                                   ? 0
                                   : processingPlaybackPreviewModeEnabled()
                                   ? ((video && video->playback_scale_factor_active > 0)
                                       ? video->playback_scale_factor_active
                                       : processingPlaybackPreviewScaleFactor())
                                   : 0,
                               llrawproc_threads,
                               &worker->diso_full20bit_scratch);
        dualiso_debug_get_full20bit_timing(
            &g_llrawproc_last_dual_iso_full20bit_timing);
        dual_iso_ms += (mlv_stage_timing_now() - dual_iso_start) * 1000.0;

        if (!dual_iso_recon_ok)
        {
            if (dual_iso_failure_backup)
            {
                memcpy(raw_image_buff, dual_iso_failure_backup, raw_image_size);
            }
            if (original_bits_per_pixel < 14)
            {
                undo_14bit(raw_image_buff,
                           raw_image_size,
                           original_bits_per_pixel);
            }
            llrawproc_restore_worker_runtime_state(
                worker,
                &worker->seeded_runtime_state);
            g_llrawproc_last_dual_iso_ms = dual_iso_ms;
            g_llrawproc_last_total_ms =
                (mlv_stage_timing_now() - apply_start) * 1000.0;
            if (using_stack_worker)
            {
                llrawproc_free_worker_state(worker);
            }
            return 0;
        }
        if (has_explicit_auto_match)
        {
            worker->diso_ev_correction = explicit_ev_correction;
            worker->diso_black_delta = explicit_black_delta;
        }

        {
            int bits_shift = 16 - raw_info.bits_per_pixel;
            worker->dng_black_level = raw_info.black_level << bits_shift;
            worker->dng_white_level = raw_info.white_level << bits_shift;
            worker->dng_bit_depth = 16;
        }

        llrawproc_worker_ensure_luts(worker, raw_info.black_level);
    }

    if (chroma_smooth_mode && dual_iso_mode != 1)
    {
        const double chroma_smooth_start = mlv_stage_timing_now();
        chroma_smooth(chroma_smooth_mode,
                      raw_image_buff,
                      override_w,
                      override_h,
                      raw_info.black_level,
                      raw_info.white_level,
                      worker->raw2ev,
                      worker->ev2raw,
                      &worker->chroma_smooth_scratch);
        chroma_smooth_ms += (mlv_stage_timing_now() - chroma_smooth_start) * 1000.0;
    }

    if (original_bits_per_pixel < 14 && dual_iso_mode != 1)
    {
        undo_14bit(raw_image_buff, raw_image_size, video->RAWI.raw_info.bits_per_pixel);
    }

    {
        const llrawproc_runtime_state_t runtime_state = llrawproc_capture_worker_runtime_state(worker);
        const int runtime_state_changed =
            !llrawproc_runtime_state_equal(&runtime_state,
                                           &worker->seeded_runtime_state,
                                           publish_auto_correction);
        if (!isolated_analysis && runtime_state_changed)
        {
            const double publish_lock_start = mlv_stage_timing_now();
            pthread_mutex_lock(&video->llrawproc_mutex);
            llrawproc_publish_worker_results(video, &runtime_state, publish_auto_correction);
            pthread_mutex_unlock(&video->llrawproc_mutex);
            g_llrawproc_last_publish_lock_ms += (mlv_stage_timing_now() - publish_lock_start) * 1000.0;
            g_llrawproc_last_shared_lock_ms += g_llrawproc_last_publish_lock_ms;
        }
    }

    g_llrawproc_last_dual_iso_ms = dual_iso_ms;
    g_llrawproc_last_chroma_smooth_ms = chroma_smooth_ms;
    g_llrawproc_last_total_ms = (mlv_stage_timing_now() - apply_start) * 1000.0;

    if (using_stack_worker)
    {
        llrawproc_free_worker_state(worker);
    }
    return 1;
}

double llrpGetLastSharedLockMilliseconds(void)
{
    return g_llrawproc_last_shared_lock_ms;
}

double llrpGetLastDualIsoRefineLockMilliseconds(void)
{
    return g_llrawproc_last_dualiso_refine_lock_ms;
}

double llrpGetLastPublishLockMilliseconds(void)
{
    return g_llrawproc_last_publish_lock_ms;
}

double llrpGetLastTotalMilliseconds(void)
{
    return g_llrawproc_last_total_ms;
}

double llrpGetLastDarkFrameMilliseconds(void)
{
    return g_llrawproc_last_dark_frame_ms;
}

double llrpGetLastVerticalStripesMilliseconds(void)
{
    return g_llrawproc_last_vertical_stripes_ms;
}

double llrpGetLastFocusPixelsMilliseconds(void)
{
    return g_llrawproc_last_focus_pixels_ms;
}

double llrpGetLastBadPixelsMilliseconds(void)
{
    return g_llrawproc_last_bad_pixels_ms;
}

double llrpGetLastPatternNoiseMilliseconds(void)
{
    return g_llrawproc_last_pattern_noise_ms;
}

double llrpGetLastPreDualIsoFixMilliseconds(void)
{
    return g_llrawproc_last_pre_dualiso_fix_ms;
}

double llrpGetLastDualIsoMilliseconds(void)
{
    return g_llrawproc_last_dual_iso_ms;
}

double llrpGetLastChromaSmoothMilliseconds(void)
{
    return g_llrawproc_last_chroma_smooth_ms;
}

void llrpGetLastDualIsoFull20bitTiming(dualiso_full20bit_timing_t * timing)
{
    if (!timing) return;
    *timing = g_llrawproc_last_dual_iso_full20bit_timing;
}

double llrpGetLastDualIsoPreviewHistogramMilliseconds(void)
{
    return g_llrawproc_last_preview_histogram_ms;
}

double llrpGetLastDualIsoPreviewRegressionMilliseconds(void)
{
    return g_llrawproc_last_preview_regression_ms;
}

double llrpGetLastDualIsoPreviewRowscaleMilliseconds(void)
{
    return g_llrawproc_last_preview_rowscale_ms;
}

void llrpResetDebugPixelMapCopyCount(void)
{
    g_llrawproc_debug_pixel_map_copy_count = 0;
}

uint64_t llrpGetDebugPixelMapCopyCount(void)
{
    return g_llrawproc_debug_pixel_map_copy_count;
}

void llrpResetDebugDarkFrameCopyCount(void)
{
    g_llrawproc_debug_dark_frame_copy_count = 0;
}

uint64_t llrpGetDebugDarkFrameCopyCount(void)
{
    return g_llrawproc_debug_dark_frame_copy_count;
}

void llrpResetDebugRuntimePublishCount(void)
{
    g_llrawproc_debug_runtime_publish_count = 0;
}

uint64_t llrpGetDebugRuntimePublishCount(void)
{
    return g_llrawproc_debug_runtime_publish_count;
}

/* Detect focus dot fix mode according to RAWC block info (binning + skipping) and camera ID
   Return value 0 = off, 1 = On, 2 = CropRec */
int llrpDetectFocusDotFixMode(mlvObject_t * video)
{
    switch(video->IDNT.cameraModel)
    {
        case 0x80000331: // EOSM
        case 0x80000355: // EOSM2
        case 0x80000346: // 100D
        case 0x80000301: // 650D
        case 0x80000326: // 700D
            if(video->RAWC.blockType[0])
            {
                int sampling_x = video->RAWC.binning_x + video->RAWC.skipping_x;
                int sampling_y = video->RAWC.binning_y + video->RAWC.skipping_y;
                if( (video->RAWI.raw_info.height < 900) && !(sampling_y == 5 && sampling_x == 3) )
                {
                    return 2;
                }
            }
            return 1;

        default: // All other cameras
            return 0;
    }
}

/* LLRawProcObject variable handling */
int llrpGetFixRawMode(mlvObject_t * video)
{
    return video->llrawproc->fix_raw;
}

void llrpSetFixRawMode(mlvObject_t * video, int value)
{
    video->llrawproc->fix_raw = value;
}

int llrpGetVerticalStripeMode(mlvObject_t * video)
{
    return video->llrawproc->vertical_stripes;
}

void llrpSetVerticalStripeMode(mlvObject_t * video, int value)
{
    video->llrawproc->vertical_stripes = value;
}

void llrpComputeStripesOn(mlvObject_t * video)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    video->llrawproc->compute_stripes = 1;
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

int llrpGetFocusPixelMode(mlvObject_t * video)
{
    return video->llrawproc->focus_pixels;
}

void llrpSetFocusPixelMode(mlvObject_t * video, int value)
{
    video->llrawproc->focus_pixels = value;
}

int llrpGetFocusPixelInterpolationMethod(mlvObject_t * video)
{
    return video->llrawproc->fpi_method;
}

void llrpSetFocusPixelInterpolationMethod(mlvObject_t * video, int value)
{
    video->llrawproc->fpi_method = value;
}

int llrpGetBadPixelMode(mlvObject_t * video)
{
    return video->llrawproc->bad_pixels;
}

void llrpSetBadPixelMode(mlvObject_t * video, int value)
{
    video->llrawproc->bad_pixels = value;
}

int llrpGetBadPixelSearchMethod(mlvObject_t *video)
{
    return video->llrawproc->bps_method;
}

void llrpSetBadPixelSearchMethod(mlvObject_t * video, int value)
{
    video->llrawproc->bps_method = value;
}

int llrpGetBadPixelInterpolationMethod(mlvObject_t * video)
{
    return video->llrawproc->bpi_method;
}

void llrpSetBadPixelInterpolationMethod(mlvObject_t * video, int value)
{
    video->llrawproc->bpi_method = value;
}

int llrpGetChromaSmoothMode(mlvObject_t * video)
{
    return video->llrawproc->chroma_smooth;
}

void llrpSetChromaSmoothMode(mlvObject_t * video, int value)
{
    video->llrawproc->chroma_smooth = value;
}

int llrpGetPatternNoiseMode(mlvObject_t * video)
{
    return video->llrawproc->pattern_noise;
}

void llrpSetPatternNoiseMode(mlvObject_t * video, int value)
{
    video->llrawproc->pattern_noise = value;
}

int llrpGetDeflickerTarget(mlvObject_t * video)
{
    return video->llrawproc->deflicker_target;
}

void llrpSetDeflickerTarget(mlvObject_t * video, int value)
{
    video->llrawproc->deflicker_target = value;
}

int llrpGetDualIsoMode(mlvObject_t * video)
{
    return video->llrawproc->dual_iso;
}

void llrpSetDualIsoMode(mlvObject_t * video, int value)
{
    video->llrawproc->dual_iso = value;
}

int llrpGetDualIsoInterpolationMethod(mlvObject_t * video)
{
    return video->llrawproc->diso_averaging;
}

void llrpSetDualIsoInterpolationMethod(mlvObject_t * video, int value)
{
    video->llrawproc->diso_averaging = value;
}

int llrpGetDualIsoPlaybackForceMean23(mlvObject_t * video)
{
    return video->llrawproc->diso_playback_force_mean23;
}

void llrpSetDualIsoPlaybackForceMean23(mlvObject_t * video, int value)
{
    video->llrawproc->diso_playback_force_mean23 = value ? 1 : 0;
}

int llrpGetDualIsoAliasMapMode(mlvObject_t * video)
{
    return video->llrawproc->diso_alias_map;
}

void llrpSetDualIsoAliasMapMode(mlvObject_t * video, int value)
{
    video->llrawproc->diso_alias_map = value;
}

int llrpGetDualIsoFullResBlendingMode(mlvObject_t * video)
{
    return video->llrawproc->diso_frblending;
}

void llrpSetDualIsoFullResBlendingMode(mlvObject_t * video, int value)
{
    video->llrawproc->diso_frblending = value;
}

int llrpGetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video)
{
    return video->llrawproc->diso_playback_force_disable_alias_map;
}

void llrpSetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video, int value)
{
    video->llrawproc->diso_playback_force_disable_alias_map = value ? 1 : 0;
}

void llrpSetDualIsoPlaybackRuntimeState(mlvObject_t * video,
                                        int mode,
                                        int interpolation,
                                        int alias_map,
                                        int fullres_blending,
                                        int playback_force_mean23,
                                        int playback_force_disable_alias_map)
{
    if (!video || !video->llrawproc) return;

    pthread_mutex_lock(&video->llrawproc_mutex);
    video->llrawproc->dual_iso = mode;
    video->llrawproc->diso_averaging = interpolation;
    video->llrawproc->diso_alias_map = alias_map;
    video->llrawproc->diso_frblending = fullres_blending;
    video->llrawproc->diso_playback_force_mean23 =
        playback_force_mean23 ? 1 : 0;
    video->llrawproc->diso_playback_force_disable_alias_map =
        playback_force_disable_alias_map ? 1 : 0;
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

int llrpGetDualIsoValidity(mlvObject_t * video)
{
    return video->llrawproc->diso_validity;
}

void llrpSetDualIsoValidity(mlvObject_t * video, int diso_force)
{
    int iso1 = (int)video->EXPO.isoValue;

    if (iso1 < 100)
    {
        iso1 = 100;
    }

    if (diso_force)
    {
        video->llrawproc->diso_validity = DISO_FORCED;

        video->llrawproc->diso1 = iso1;
        video->llrawproc->diso2 = iso1;
    }
    else if (video->DISO.blockType[0] && video->DISO.dualMode)
    {
        video->llrawproc->diso_validity = DISO_VALID;

        int iso2 = (int)video->DISO.isoValue;

        if (iso2 < 0)
        {
            if (iso2 < -6)
            {
                iso2 = iso1 / pow(2, ABS(iso2) - 6);
            }
            else
            {
                iso2 = iso1 * pow(2, ABS(7 + iso2));
            }

            iso2 = COERCE(iso2, 100, 3200);
        }
        else if ((iso2 >= 0) && (iso2 < 100))
        {
            iso2 = iso1 * pow(2, iso2) / (iso1 / 100);
        }

        video->llrawproc->diso1 = iso1;
        video->llrawproc->diso2 = iso2;
    }
    else
    {
        video->llrawproc->diso_validity = DISO_INVALID;
    }
}

int llrpHQDualIso(mlvObject_t * video)
{
    return (video->llrawproc->dual_iso == 1) && video->llrawproc->diso_validity && (llrpGetFixRawMode(video));
}

/* Seed shared->diso_pattern from a FULL-RESOLUTION raw frame when it is still
 * unseeded (0). The scaled (reduced-resolution) playback recon detects the
 * dual-ISO bright/dark row pattern from the data it is handed; on 8x/4x
 * downsampled data that detection is unreliable (the row distinction is
 * averaged away), so on the cold pass — before any full-res render has seeded
 * the pattern, e.g. the processed8 prefetch worker reconstructing scaled frames
 * at play start — it mis-detects and emits the green-dropped "x8 cold pink".
 * Detecting once from the full-res raw (row pattern intact) and publishing the
 * SAME negative -1..-4 encoding that diso_get_full20bit / diso_get_preview both
 * consume gives the scaled recon a correct seed. diso_check=1 is detect-only and
 * READ-ONLY on raw_full. No-op when not dual-ISO, already seeded, or detection
 * fails — so it can only improve, never regress (and never returns failure to a
 * caller, unlike the reverted defer guard). */
void llrpEnsureDualIsoPatternSeeded(mlvObject_t * video, uint16_t * raw_full, int full_w, int full_h)
{
    if (!video || !video->llrawproc || !raw_full) return;
    llrawprocObject_t * shared = video->llrawproc;
    if (!shared->fix_raw || !shared->diso_validity || shared->dual_iso != 1) return;
    if (shared->diso_pattern != 0) return;
    if (full_w <= 0 || full_h <= 0 || full_w > 65535 || full_h > 65535) return;

    int detected = 0;
    if (diso_get_preview(raw_full,
                         (uint16_t)full_w,
                         (uint16_t)full_h,
                         (int32_t)video->RAWI.raw_info.black_level,
                         (int32_t)video->RAWI.raw_info.white_level,
                         &detected,
                         1 /* diso_check: detect-only, read-only on raw_full */,
                         NULL)
        && detected != 0)
    {
        pthread_mutex_lock(&video->llrawproc_mutex);
        if (shared->diso_pattern == 0)
        {
            shared->diso_pattern = detected;
        }
        pthread_mutex_unlock(&video->llrawproc_mutex);

        static int s_log_diso_seed = -1;
        if (s_log_diso_seed < 0)
        {
            const char * v = getenv("MLVAPP_LOG_DISO_SEED");
            s_log_diso_seed = (v && v[0] && v[0] != '0') ? 1 : 0;
        }
        if (s_log_diso_seed)
        {
            fprintf(stderr, "[DISO_SEED] seeded diso_pattern=%d from full-res %dx%d\n",
                    detected, full_w, full_h);
        }
    }
}

void llrpResetDngBWLevels(mlvObject_t * video)
{
    video->llrawproc->dng_bit_depth = video->RAWI.raw_info.bits_per_pixel;
    video->llrawproc->dng_black_level = video->RAWI.raw_info.black_level;
    video->llrawproc->dng_white_level = video->RAWI.raw_info.white_level;
}

void llrpResetFpmStatus(mlvObject_t * video)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    reset_fpm_status(&(video->llrawproc->focus_pixel_map), &(video->llrawproc->fpm_status));
    llrawproc_bump_focus_map_version(video->llrawproc);
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

void llrpResetBpmStatus(mlvObject_t * video)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    reset_bpm_status(&(video->llrawproc->bad_pixel_map), &(video->llrawproc->bpm_status));
    llrawproc_bump_bad_map_version(video->llrawproc);
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

/* dark frame stuff */
void llrpInitDarkFrameExtFileName(mlvObject_t * video, char * df_filename)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    const int changed = !video->llrawproc->dark_frame_filename
                     || strcmp(video->llrawproc->dark_frame_filename, df_filename) != 0;
    df_free_filename(video);
    df_init_filename(video, df_filename);
    if (changed) df_free(video);
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

void llrpFreeDarkFrameExtFileName(mlvObject_t * video)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    const int changed = video->llrawproc->dark_frame_filename != NULL;
    df_free_filename(video);
    if (changed) df_free(video);
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

int llrpGetDarkFrameMode(mlvObject_t * video)
{
    return video->llrawproc->dark_frame;
}

void llrpSetDarkFrameMode(mlvObject_t * video, int value)
{
    pthread_mutex_lock(&video->llrawproc_mutex);
    if (video->llrawproc->dark_frame != value)
    {
        video->llrawproc->dark_frame = value;
        df_free(video);
    }
    else
    {
        video->llrawproc->dark_frame = value;
    }
    pthread_mutex_unlock(&video->llrawproc_mutex);
}

int llrpGetDarkFrameExtStatus(mlvObject_t * video)
{
    if(video->llrawproc->dark_frame_filename) return 1;
    return 0;
}

int llrpGetDarkFrameIntStatus(mlvObject_t * video)
{
    if(video->DARK.blockType[0]) return 1;
    return 0;
}

int llrpValidateExtDarkFrame(mlvObject_t * video, char * df_filename, char * error_message)
{
    return df_validate(video, df_filename, error_message);
}
