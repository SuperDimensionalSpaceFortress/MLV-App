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
#include <string.h>

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
/* While enabled on the calling thread, an ISOLATED analysis render (the two entry points above) reads the shared
 * object but never writes it: focus/bad pixel map preparation, their versions and status, the force-search reset and
 * the vertical-stripe one-shot all land in a private per-thread shadow. Off by default; only measure-only analysis
 * (the Look Assist window-lit verification) turns it on. Returns the previous value. */
int llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread(int enabled);
/* LOOK-ASSIST-M16-CAST-3: the dual-ISO exposure match an ISOLATED analysis render on the calling thread seeds with.
 * SEED (the default) is today's nominal seed (auto -1, ev 1, black delta -1); MEASURED runs the histogram match (-2);
 * EXPLICIT uses ev_correction (stops, negative, the shared field's convention) and black_delta (14-bit units). Only
 * Look Assist's dual-ISO match probe sets it, and it puts the previous mode back. Returns the previous mode. */
#define LLRP_ANALYSIS_DISO_MATCH_SEED     0
#define LLRP_ANALYSIS_DISO_MATCH_MEASURED 1
#define LLRP_ANALYSIS_DISO_MATCH_EXPLICIT 2
int llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread(int mode, double ev_correction, int black_delta);
/* LOOK-ASSIST-M16-CAST-3 measure-only: the HQ dual-ISO recon options an ISOLATED analysis render on the calling thread
 * uses instead of the shared ones (interp 0 AMaZE / 1 mean23, alias map, fullres blending, chroma smoothing method).
 * -1 keeps the shared value; all -1 (the default) is today's render. */
void llrpSetIsolatedAnalysisDualIsoReconForCurrentThread(int interp, int alias_map, int fullres, int chroma_smooth);
/* LOOK-ASSIST-M16-CAST-4 measure-only arms for an ISOLATED, read-only analysis render on the calling thread (never a
 * live render): dual_iso_mode 1 (HQ) / 2 (the preview recon), else -1 = the shared mode; white_bright (14-bit, > 0)
 * replaces the HQ recon's assumed bright clip white/2; dark_noise_scale (> 0) multiplies its dark noise; a non-NULL
 * dark_black_offset (14-bit codes per CFA channel R, G1, G2, B) is subtracted from its dark-field rows. -1 / 0 / 0.0 /
 * NULL is today's render; the caller resets after every render. The getter reports the current arms (1 = any set). */
void llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread(int dual_iso_mode,
                                                        int white_bright,
                                                        double dark_noise_scale,
                                                        const int * dark_black_offset);
int llrpGetIsolatedAnalysisDualIsoArmsForCurrentThread(int * dual_iso_mode,
                                                       int * white_bright,
                                                       double * dark_noise_scale,
                                                       int * dark_black_offset_enabled);
/* LOOK-ASSIST-M16-CAST-5 measure-only switch arms, same contract (ISOLATED analysis renders on the calling thread only,
 * handed to the recon for its duration). Call AFTER llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread, which clears
 * them. channel_ev / channel_bd (both non-NULL and finite: per CFA channel R, G1, G2, B, stops positive and 14-bit
 * codes) darken each bright-field channel on its own (A11); quad_coherent_switch ORs the overexposure switch across
 * each 2x2 CFA quad (A12); capture_switch_maps records each switch site's outcome (dualiso_switch_capture_map, P1).
 * NULL / 0 is today's render. The getter reports them (1 = any set). */
void llrpSetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread(const double * channel_ev,
                                                              const double * channel_bd,
                                                              int quad_coherent_switch,
                                                              int capture_switch_maps);
int llrpGetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread(int * channel_match, int * quad_coherent_switch,
                                                             int * capture_switch_maps);
/* LOOK-ASSIST-M16-CAST-6 measure-only, same contract; call AFTER the switch-arms setter, which clears them.
 * channel_mask (bit c = CFA channel R, G1, G2, B) limits A11 to those channels, the others keep the global match
 * (0 = all four); channel_bd_global gives every A11 channel the global black delta (A11s); capture_output records the HQ
 * recon's 16-bit Bayer output (dualiso_output_capture). The getter reports them (1 = any set). */
void llrpSetIsolatedAnalysisDualIsoChannelArmsForCurrentThread(int channel_mask, int channel_bd_global, int capture_output);
int llrpGetIsolatedAnalysisDualIsoChannelArmsForCurrentThread(int * channel_mask, int * channel_bd_global,
                                                              int * capture_output);
/* LOOK-ASSIST-M16-CAST-6 X0 (ISOLATED analysis runs on the calling thread only): steps_off skips every pre-dual-ISO step
 * (dark frame, vertical stripes, focus pixels, bad pixels, pattern noise) and tells the recon no dark frame was
 * subtracted; record_hash hashes the buffer handed to dual ISO. Cleared by
 * llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread (set it after that call). Returns the previous steps_off. The
 * getter reports the last isolated run on this thread: the steps enabled (bit 1 dark frame, 2 vertical stripes, 4 focus
 * pixels, 8 bad pixels, 16 pattern noise; before X0), the steps that ran, and the hash (0 unless asked); it returns the
 * current steps_off. */
int llrpSetIsolatedAnalysisPreDualIsoForCurrentThread(int steps_off, int record_hash);
int llrpGetLastPreDualIsoForCurrentThread(int * enabled_mask, int * applied_mask, unsigned long long * buffer_hash);
/* The output levels (dng bit depth / black / white) of the last llrawproc run on the calling thread that completed for
 * `video`, live or isolated: the levels its frame is at, which a display render syncs the processing object to. Returns
 * 0 when no run completed for `video` since the last reset on this thread (e.g. the frame came from a cache). */
void llrpResetLastOutputLevelsForCurrentThread(void);
int llrpLastOutputLevelsForCurrentThread(const mlvObject_t * video, int * bit_depth, int * black_level, int * white_level);
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
    /* Zero-based identity of the frame this state was prepared for. The async-H2D
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

/* Render-worker-thread-only: the preupload status of whichever recon call
 * applyLLRawProcObjectWorker() itself just made on THIS thread (retained-
 * device or synchronous-CPU16 playback path). Same-thread producer and
 * consumer -- see the comment on the backing TLS in llrawproc.c. */
int llrpGpuPlaybackReconLastPreuploadStatusForTesting(
    llrpGpuPlaybackReconPreuploadStatus_t * status);

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

/* The CUDA slot API reserves zero for an unavailable token. Both producer and
 * consumer translate the public zero-based frame identity at this boundary.
 * UINT64_MAX cannot be represented without aliasing zero: use synchronous work. */
static inline uint64_t llrpGpuPlaybackReconFrameToken(uint64_t frame_id)
{
    return frame_id == UINT64_MAX ? 0 : frame_id + 1u;
}

/* Shared compare-and-reject: MainWindow calls this with the frame identity a
 * prepared llrpGpuPlaybackReconState_t was built for and the frame actually
 * being presented. A mismatch means the prepared state does not provably
 * belong to this frame, so this returns the UINT64_MAX "not armed" sentinel
 * (llrpGpuPlaybackReconFrameToken() maps it to token 0) instead of the
 * prepared frame_id -- both llrawproc_gpu_recon_run_backend()'s token gate
 * and GpuDisplayViewport/Window's retained-device-buffer gate key off the
 * same sentinel, so this one function is the single source of the decision
 * both of them act on. Pulled out to a pure, header-only function (rather
 * than left inline in MainWindow.cpp) specifically so it is unit-testable
 * without a GUI harness. */
static inline uint64_t llrpGpuPlaybackReconFrameIdAfterCompareAndReject(
    uint64_t prepared_frame_id, uint64_t display_frame_id)
{
    return prepared_frame_id == display_frame_id ? prepared_frame_id : UINT64_MAX;
}

/* Shared by both display adapters (GpuDisplayViewport.cpp, GpuDisplayWindow.cpp),
 * at both the public entry point and the internal submit function that backs
 * it, so there is exactly one implementation of "is this retained device
 * buffer usable for this frame" for all of them to key off. Previously each
 * of those call sites reimplemented this boolean inline, which let sol's
 * pre-review #2 hardening finding stand: a regression in any one copy had no
 * automated test, because the tests/gui directory (the only harness that can
 * instantiate a real adapter) is an excluded path for this card, and the pure-function
 * compare-and-reject tests never invoked an adapter. Pulling this out here
 * makes it directly unit-testable without a GUI harness, the same reasoning
 * that pulled llrpGpuPlaybackReconFrameIdAfterCompareAndReject() out above --
 * a mismatched frame's compare-and-reject leaves frame_id at the UINT64_MAX
 * "not armed" sentinel, which llrpGpuPlaybackReconFrameToken() maps to token
 * 0, and that must disqualify the retained-buffer shortcut exactly as it
 * disqualifies the run_backend() token gate. */
static inline int llrpGpuPlaybackReconRetainedDeviceBufferValid(
    const uint16_t * retained_device_bayer16,
    int retained_device_width,
    int retained_device_height,
    int expected_width,
    int expected_height,
    int validation_probe_texture,
    uint64_t state_frame_id)
{
    return retained_device_bayer16 != NULL
        && retained_device_width == expected_width
        && retained_device_height == expected_height
        && !validation_probe_texture
        && llrpGpuPlaybackReconFrameToken( state_frame_id ) != 0;
}

/* Shared by both display adapters. Preupload status belongs to the recon run,
 * including when its scalar timer is unavailable. A retained-device handoff
 * without a recon call supplies a zero-initialized recon timing here. */
static inline llrpGpuPlaybackReconTiming_t llrpGpuPlaybackReconCombineTiming(
    const llrpGpuPlaybackReconTiming_t * recon,
    int debayer_available, double upload_ms, double kernel_ms,
    double interop_ms, double total_ms)
{
    llrpGpuPlaybackReconTiming_t combined;
    memset(&combined, 0, sizeof(combined));
    combined.preupload = recon->preupload;
    combined.available = recon->available || debayer_available;
    combined.upload_ms = (recon->available ? recon->upload_ms : 0.0)
        + (debayer_available ? upload_ms : 0.0);
    combined.kernel_ms = (recon->available ? recon->kernel_ms : 0.0)
        + (debayer_available ? kernel_ms : 0.0);
    combined.interop_ms = (recon->available ? recon->interop_ms : 0.0)
        + (debayer_available ? interop_ms : 0.0);
    combined.total_ms = (recon->available ? recon->total_ms : 0.0)
        + (debayer_available ? total_ms : 0.0);
    return combined;
}

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
/* Test-only synchronous observer: substitutes for upload on the current thread.
 * The observer must copy the bytes before returning; NULL restores real upload. */
typedef void (*llrpGpuPlaybackPreuploadObserver_t)(uint64_t, const uint16_t *, size_t);
void llrpSetGpuPlaybackPreuploadObserverForTesting(llrpGpuPlaybackPreuploadObserver_t observer);
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

/* CPU-DUALISO-AT-PREVIEW-SCALE-1: applyLLRawProcObject_with_dims on a caller-
 * owned worker state (the phase-3 recon worker's), with flags:
 *  - FULLRES_FIXES_APPLIED: the coordinate-sensitive fixes (focus, bad pixels,
 *    vertical stripes, pattern noise) already ran on the full-resolution frame,
 *    so their being enabled does not reject the scaled call;
 *  - NO_PUBLISH: the dual-ISO runtime state (pattern, auto/ev correction, black
 *    delta) estimated on the reduced buffer is never published to the shared
 *    llrawproc object that exports and paused frames read. */
#define LLRP_WITH_DIMS_FULLRES_FIXES_APPLIED 0x1
#define LLRP_WITH_DIMS_NO_PUBLISH 0x2
int applyLLRawProcObjectWorker_with_dims(mlvObject_t * video,
                                         uint16_t * raw_image_buff,
                                         size_t raw_image_size,
                                         int override_w,
                                         int override_h,
                                         llrawprocWorkerState_t * worker,
                                         int flags);
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
int llrpGetLastPreDualIsoFixCompleted(void);
void llrpResetLastPreDualIsoFixTelemetry(void);
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
