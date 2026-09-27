/*
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

#ifndef _llrawproc_h
#define _llrawproc_h

#include <stddef.h>
#include <stdint.h>

#include "llrawproc_object.h"
#include "../mlv_object.h"

#ifdef __cplusplus
extern "C" {
#endif

llrawprocObject_t * initLLRawProcObject();
void freeLLRawProcObject(mlvObject_t * video);

/* all low level raw processing takes place here */
void applyLLRawProcObject(mlvObject_t * video, uint16_t * raw_image_buff, size_t raw_image_size);
void llrpInitWorkerState(llrawprocWorkerState_t * worker);
void llrpFreeWorkerState(llrawprocWorkerState_t * worker);
void applyLLRawProcObjectWorker(mlvObject_t * video,
                                uint16_t * raw_image_buff,
                                size_t raw_image_size,
                                llrawprocWorkerState_t * worker,
                                int stop_before_dual_iso);
void applyLLRawProcObjectWorkerIsolatedAnalysis(mlvObject_t * video,
                                                uint16_t * raw_image_buff,
                                                size_t raw_image_size,
                                                llrawprocWorkerState_t * worker,
                                                int stop_before_dual_iso);
void applyLLRawProcObjectWorkerIsolatedAnalysisWithChromaSmooth(mlvObject_t * video,
                                                                uint16_t * raw_image_buff,
                                                                size_t raw_image_size,
                                                                llrawprocWorkerState_t * worker,
                                                                int stop_before_dual_iso,
                                                                int chroma_smooth_method);
void llrpSetGpuPlaybackReconAllowedForCurrentThread(int enabled);
void llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(int enabled);
void llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(int enabled);
int llrpResetGpuPlaybackReconRunForTesting(void);
int llrpGpuPlaybackReconLastRunAttemptedForTesting(void);
int llrpGpuPlaybackReconLastRunRcForTesting(void);
int llrpGpuPlaybackReconLastUsedForTesting(void);
int llrpGpuPlaybackReconLastStateValidForTesting(void);
int llrpGpuPlaybackReconLastPrepareOnlyForTesting(void);

#define LLRP_GPU_PLAYBACK_RECON_RAW2EV_COUNT (1u << 20)
#define LLRP_GPU_PLAYBACK_RECON_EV2RAW_COUNT (24u * 65536u)
#define LLRP_GPU_PLAYBACK_RECON_RANDN05_COUNT 1024u
#define LLRP_GPU_PLAYBACK_RECON_RC_UNSUPPORTED_STATE 3

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
    /* Identity of the frame this state was prepared for. The async-H2D
     * preupload gate in llrawproc_gpu_recon_run_backend() keys on this value
     * to decide whether a previously host-staged upload matches the frame
     * about to be reconstructed. It MUST be supplied explicitly by whichever
     * side builds this struct (RenderFrameThread.cpp / MainWindow.cpp) --
     * it used to travel as an ambient MLV_THREAD_LOCAL armed on the render
     * worker thread and read on the GL presentation thread, which never
     * bridged the two and left async H2D permanently disarmed. See
     * .claude-state/project-memory/async-h2d-frameid-crosses-a-subsystem-boundary-20260905.md. */
    uint64_t frame_id;
} llrpGpuPlaybackReconState_t;

typedef struct
{
    int available;
    int accepted;
    int used;
    int exact_match;
    int submitted_while_prior_run_active;
    int ready_before_run;
    double host_staging_ms;
    double upload_ms;
    double upload_wait_ms;
} llrpGpuPlaybackReconPreuploadStatus_t;

typedef struct
{
    int available;
    double upload_ms;
    double kernel_ms;
    double interop_ms;
    double total_ms;
    double wall_ms;
    double host_gap_ms;
    double context_ms;
    double setup_ms;
    double recon_wall_ms;
    double amaze_wall_ms;
    double post_ms;
    /* Populated from the SAME run_backend() invocation that filled the
     * timing fields above -- an explicit OUT param, not the old ambient
     * thread-local, so it is safe to read from any thread that owns this
     * timing_out pointer. */
    llrpGpuPlaybackReconPreuploadStatus_t preupload;
} llrpGpuPlaybackReconTiming_t;

typedef struct
{
    int valid;
    const uint16_t * device_bayer16;
    int width;
    int height;
    uint64_t token;
} llrpGpuPlaybackRetainedDeviceBayer16_t;

typedef struct
{
    int available;
    int attempted;
    int unavailable;
    char requested_backend[128];
    char requested_path[1024];
    char resolved_path[1024];
    char description[1024];
} llrpGpuPlaybackReconBackendInfo_t;

int llrpGpuPlaybackReconGetLastPreparedState(llrpGpuPlaybackReconState_t * state);
size_t llrpGpuPlaybackReconGetLastInputBayer16(uint16_t * output,
                                               size_t output_words);
int llrpGpuPlaybackReconGetBackendInfo(llrpGpuPlaybackReconBackendInfo_t * info);
int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size);
int llrpGpuPlaybackReconResetGlTextureResources(void);
int llrpGpuPlaybackReconRunGlTexture(const llrpGpuPlaybackReconState_t * state,
                                     const uint16_t * raw_input_bayer14,
                                     size_t raw_image_size,
                                     unsigned int gl_texture_id,
                                     int * rc_out,
                                     llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunDeviceBayer16(const llrpGpuPlaybackReconState_t * state,
                                         const uint16_t * raw_input_bayer14,
                                         size_t raw_image_size,
                                         const uint16_t ** device_bayer16_out,
                                         int * width_out,
                                         int * height_out,
                                         int * rc_out,
                                         llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconRunRetainedDeviceBayer16(
    const llrpGpuPlaybackReconState_t * state,
    const uint16_t * raw_input_bayer14,
    size_t raw_image_size,
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out,
    int * rc_out,
    llrpGpuPlaybackReconTiming_t * timing_out);
int llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(
    llrpGpuPlaybackRetainedDeviceBayer16_t * retained_out);
int llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(uint64_t token);
int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,
                                                         int * rc_out);
int llrpGpuPlaybackReconRunCpu16Probe(const llrpGpuPlaybackReconState_t * state,
                                      const uint16_t * raw_input_bayer14,
                                      size_t raw_image_size,
                                      uint16_t * output_bayer16,
                                      int * rc_out,
                                      llrpGpuPlaybackReconTiming_t * timing_out);

typedef struct
{
    int attempted;
    int rc;
    int replaced;
    int trusted;
    int allocated_bytes_valid;
    uint64_t allocated_bytes;
    int skip_code;
} llrpGpuExportTelemetry_t;

typedef enum
{
    LLRP_GPU_EXPORT_SKIP_NONE = 0,
    LLRP_GPU_EXPORT_SKIP_DISABLED = 1,
    LLRP_GPU_EXPORT_SKIP_NOT_DUAL_ISO = 2,
    LLRP_GPU_EXPORT_SKIP_EMPTY_RAW_IMAGE = 3,
    LLRP_GPU_EXPORT_SKIP_BACKEND_UNAVAILABLE = 4,
    LLRP_GPU_EXPORT_SKIP_INPUT_ALLOC_FAILED = 5,
    LLRP_GPU_EXPORT_SKIP_PLAYBACK_RECON_USED = 6,
    LLRP_GPU_EXPORT_SKIP_DUAL_ISO_RECON_FAILED = 7,
    LLRP_GPU_EXPORT_SKIP_MISSING_INPUT = 8,
    LLRP_GPU_EXPORT_SKIP_MISSING_RECON_STATE = 9,
    LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE = 10,
    LLRP_GPU_EXPORT_SKIP_SIZE_MISMATCH = 11,
    LLRP_GPU_EXPORT_SKIP_OUTPUT_ALLOC_FAILED = 12,
    LLRP_GPU_EXPORT_SKIP_COUNT = 13
} llrpGpuExportSkipCode_t;

void llrpGetLastGpuExportTelemetry(llrpGpuExportTelemetry_t * telemetry);

/* Playback-only helper: apply the full-res raw coordinate/fix stages and stop
 * before Dual ISO recon. Used by the x4 quality-preserving early-reduction
 * prototype so the raw fixes can happen before the reduced path runs. */
void applyLLRawProcObjectPreDualIsoFixes(mlvObject_t * video,
                                         uint16_t * raw_image_buff,
                                         size_t raw_image_size);

/* Phase 4B-v2: scaled variant for the downsample-BEFORE-llrawproc path.
 * Runs the same pipeline as applyLLRawProcObject but on a buffer whose
 * dimensions differ from video->RAWI.xRes / yRes. The override_w and
 * override_h MUST match the actual buffer dimensions (rows*cols*sizeof
 * uint16_t == raw_image_size). The 4-row dual-ISO pattern must be
 * preserved by the caller — the recon code reads is_bright[y%4] and the
 * caller's downsample kernel must guarantee row 0,1,2,3 of the input
 * carry the same brightness pattern as the corresponding source rows.
 *
 * Subset of operations applied:
 *  - dark frame subtraction (size-agnostic)
 *  - HQ Dual ISO recon (operates on raw_info.width/height)
 *  - chroma smooth (size-agnostic)
 *  - 14-bit lift / undo
 * NOT applied (pre-downsample at full res only when enabled, or skipped by
 * Aggressive Performance preview):
 *  - focus pixel interpolation (uses absolute sensor coords)
 *  - bad pixel interpolation (same)
 *  - vertical stripes (uses absolute column index)
 *  - pattern noise fix
 *
 * Returns 1 if the scaled application is safe or intentionally approximate
 * for aggressive preview, 0 if not (in which case the caller must fall back
 * to the v1 full-res llrawproc + post-downsample path). */
int applyLLRawProcObject_with_dims(mlvObject_t * video,
                                   uint16_t * raw_image_buff,
                                   size_t raw_image_size,
                                   int override_w,
                                   int override_h);
double llrpGetLastSharedLockMilliseconds(void);
double llrpGetLastDualIsoRefineLockMilliseconds(void);
double llrpGetLastPublishLockMilliseconds(void);
double llrpGetLastTotalMilliseconds(void);
double llrpGetLastDarkFrameMilliseconds(void);
double llrpGetLastVerticalStripesMilliseconds(void);
double llrpGetLastFocusPixelsMilliseconds(void);
double llrpGetLastBadPixelsMilliseconds(void);
double llrpGetLastPatternNoiseMilliseconds(void);
double llrpGetLastPreDualIsoFixMilliseconds(void);
double llrpGetLastDualIsoMilliseconds(void);
double llrpGetLastChromaSmoothMilliseconds(void);
void llrpGetLastDualIsoFull20bitTiming(dualiso_full20bit_timing_t * timing);
double llrpGetLastDualIsoPreviewHistogramMilliseconds(void);
double llrpGetLastDualIsoPreviewRegressionMilliseconds(void);
double llrpGetLastDualIsoPreviewRowscaleMilliseconds(void);
void llrpResetDebugPixelMapCopyCount(void);
uint64_t llrpGetDebugPixelMapCopyCount(void);
void llrpResetDebugDarkFrameCopyCount(void);
uint64_t llrpGetDebugDarkFrameCopyCount(void);
void llrpResetDebugRuntimePublishCount(void);
uint64_t llrpGetDebugRuntimePublishCount(void);

/* Detect focus dot fix mode according to RAWC block info (binning + skipping) and camera ID
   Return value 0 = off, 1 = On, 2 = CropRec */
int llrpDetectFocusDotFixMode(mlvObject_t * video);

/* LLRawProcObject all member variable handling functions */
enum { FR_OFF, FR_ON };
int llrpGetFixRawMode(mlvObject_t * video);
void llrpSetFixRawMode(mlvObject_t * video, int value);

enum { VS_OFF, VS_ON, VS_FORCE };
int llrpGetVerticalStripeMode(mlvObject_t * video);
void llrpSetVerticalStripeMode(mlvObject_t * video, int value);
void llrpComputeStripesOn(mlvObject_t * video);

enum { FP_OFF, FP_ON, FP_CROPREC };
int llrpGetFocusPixelMode(mlvObject_t * video);
void llrpSetFocusPixelMode(mlvObject_t * video, int value);

enum { FPI_MLVFS, FPI_RAW2DNG };
int llrpGetFocusPixelInterpolationMethod(mlvObject_t * video);
void llrpSetFocusPixelInterpolationMethod(mlvObject_t * video, int value);

enum { BP_OFF, BP_ON, FP_AGGRESSIVE };
int llrpGetBadPixelMode(mlvObject_t * video);
void llrpSetBadPixelMode(mlvObject_t * video, int value);

enum { BPS_NORMAL, BPS_FORCE };
int llrpGetBadPixelSearchMethod(mlvObject_t * video);
void llrpSetBadPixelSearchMethod(mlvObject_t * video, int value);

enum { BPI_MLVFS, BPI_RAW2DNG };
int llrpGetBadPixelInterpolationMethod(mlvObject_t * video);
void llrpSetBadPixelInterpolationMethod(mlvObject_t * video, int value);

enum { CS_OFF, CS_2x2, CS_3x3, CS_5x5 };
int llrpGetChromaSmoothMode(mlvObject_t * video);
void llrpSetChromaSmoothMode(mlvObject_t * video, int value);

enum { PN_OFF, PN_ON };
int llrpGetPatternNoiseMode(mlvObject_t * video);
void llrpSetPatternNoiseMode(mlvObject_t * video, int value);

int llrpGetDeflickerTarget(mlvObject_t * video);
void llrpSetDeflickerTarget(mlvObject_t * video, int value);

/* dual iso stuff */
enum { DISO_OFF, DISO_20BIT, DISO_FAST };
int llrpGetDualIsoMode(mlvObject_t * video);
void llrpSetDualIsoMode(mlvObject_t * video, int value);

enum { DISOI_AMAZE, DISOI_MEAN23 };
int llrpGetDualIsoInterpolationMethod(mlvObject_t * video);
void llrpSetDualIsoInterpolationMethod(mlvObject_t * video, int value);

/* Playback-only override: when non-zero, applyLLRawProcObject forces the
 * HQ dual ISO recon (dual_iso == DISO_20BIT) onto the mean23 interpolation
 * regardless of what the receipt selected. The receipt's stored value is
 * not modified, so paused/scrubbing/export keep the authored choice (e.g.
 * AMaZE). Set/cleared by the GUI in MainWindow::applyEffectiveDualIsoPlaybackSettings
 * based on effectiveDualIsoPlaybackRuntimeSettings(). */
int llrpGetDualIsoPlaybackForceMean23(mlvObject_t * video);
void llrpSetDualIsoPlaybackForceMean23(mlvObject_t * video, int value);

int llrpGetDualIsoAliasMapMode(mlvObject_t * video);
void llrpSetDualIsoAliasMapMode(mlvObject_t * video, int value);

int llrpGetDualIsoFullResBlendingMode(mlvObject_t * video);
void llrpSetDualIsoFullResBlendingMode(mlvObject_t * video, int value);

/* Phase E5: playback-only overrides that force the HQ dual ISO recon to
 * skip alias_map suppression and full-res blending. The GUI sets these
 * non-zero when playback is active AND HQ recon would run AND the active
 * playback scale factor is >= 4 (see MainWindow::applyEffectiveDualIsoPlaybackSettings).
 * Receipt-authored values for diso_alias_map / diso_frblending are not
 * modified, so paused/scrubbing/export still apply the receipt's intended
 * quality. Diagnostic env var MLVAPP_PLAYBACK_KEEP_ALIAS_MAP_AT_SCALE=1
 * forces the override off (matches the precedent set by the rowscale and
 * mean23 escape hatches). */
int llrpGetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video);
void llrpSetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video, int value);

/* Playback runtime fast/quality state can flip while worker threads are
 * rendering. Apply the coupled fields under one llrawproc mutex so workers
 * never capture a half-HQ/half-fast state. */
void llrpSetDualIsoPlaybackRuntimeState(mlvObject_t * video,
                                        int mode,
                                        int interpolation,
                                        int alias_map,
                                        int fullres_blending,
                                        int playback_force_mean23,
                                        int playback_force_disable_alias_map);

enum { DISO_INVALID, DISO_FORCED, DISO_VALID }; // Return values
int llrpGetDualIsoValidity(mlvObject_t * video);
void llrpSetDualIsoValidity(mlvObject_t * video, int diso_force);

int llrpHQDualIso(mlvObject_t * video);

/* Detect+seed shared->diso_pattern from a full-res raw frame when unseeded, so
 * the scaled playback recon does not mis-detect the dual-ISO row pattern from
 * downsampled data (the x8/x4 cold pink). No-op when not dual-ISO, already
 * seeded, or detection fails. */
void llrpEnsureDualIsoPatternSeeded(mlvObject_t * video, uint16_t * raw_full, int full_w, int full_h);

void llrpResetDngBWLevels(mlvObject_t * video);

/* reset focus/bad pixel map status */
void llrpResetFpmStatus(mlvObject_t * video);
void llrpResetBpmStatus(mlvObject_t * video);

/* dark frame stuff */
void llrpInitDarkFrameExtFileName(mlvObject_t * video, char * df_filename);
void llrpFreeDarkFrameExtFileName(mlvObject_t * video);

int llrpGetDarkFrameMode(mlvObject_t * video);
void llrpSetDarkFrameMode(mlvObject_t * video, int value);

int llrpGetDarkFrameExtStatus(mlvObject_t * video);
int llrpGetDarkFrameIntStatus(mlvObject_t * video);

int llrpValidateExtDarkFrame(mlvObject_t * video, char * df_filename, char * error_message);

#ifdef __cplusplus
}
#endif

#endif
