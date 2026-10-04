#include <cstdint>
#include <cstdlib>

#include "../../src/mlv_include.h"
extern "C" {
#include "../../src/debayer/wb_conversion.h"
#include "../../src/debayer/debayer.h"
}
#ifndef restrict
#define restrict __restrict
#endif
#include "../../src/librtprocess/src/include/librtprocesswrapper.h"

#include <cstring>

extern "C" {

int llrpDetectFocusDotFixMode(mlvObject_t * video)
{
    switch(video->IDNT.cameraModel)
    {
        case 0x80000331:
        case 0x80000355:
        case 0x80000346:
        case 0x80000301:
        case 0x80000326:
            return 1;
        default:
            return 0;
    }
}

int llrpGetFixRawMode(mlvObject_t * video) { return video->llrawproc->fix_raw; }
void llrpSetFixRawMode(mlvObject_t * video, int value) { video->llrawproc->fix_raw = value; }
int llrpGetVerticalStripeMode(mlvObject_t * video) { return video->llrawproc->vertical_stripes; }
void llrpSetVerticalStripeMode(mlvObject_t * video, int value) { video->llrawproc->vertical_stripes = value; }
void llrpComputeStripesOn(mlvObject_t * video) { video->llrawproc->compute_stripes = 1; }
int llrpGetFocusPixelMode(mlvObject_t * video) { return video->llrawproc->focus_pixels; }
void llrpSetFocusPixelMode(mlvObject_t * video, int value) { video->llrawproc->focus_pixels = value; }
int llrpGetFocusPixelInterpolationMethod(mlvObject_t * video) { return video->llrawproc->fpi_method; }
void llrpSetFocusPixelInterpolationMethod(mlvObject_t * video, int value) { video->llrawproc->fpi_method = value; }
int llrpGetBadPixelMode(mlvObject_t * video) { return video->llrawproc->bad_pixels; }
void llrpSetBadPixelMode(mlvObject_t * video, int value) { video->llrawproc->bad_pixels = value; }
int llrpGetBadPixelSearchMethod(mlvObject_t * video) { return video->llrawproc->bps_method; }
void llrpSetBadPixelSearchMethod(mlvObject_t * video, int value) { video->llrawproc->bps_method = value; }
int llrpGetBadPixelInterpolationMethod(mlvObject_t * video) { return video->llrawproc->bpi_method; }
void llrpSetBadPixelInterpolationMethod(mlvObject_t * video, int value) { video->llrawproc->bpi_method = value; }
int llrpGetChromaSmoothMode(mlvObject_t * video) { return video->llrawproc->chroma_smooth; }
void llrpSetChromaSmoothMode(mlvObject_t * video, int value) { video->llrawproc->chroma_smooth = value; }
int llrpGetPatternNoiseMode(mlvObject_t * video) { return video->llrawproc->pattern_noise; }
void llrpSetPatternNoiseMode(mlvObject_t * video, int value) { video->llrawproc->pattern_noise = value; }
int llrpGetDeflickerTarget(mlvObject_t * video) { return video->llrawproc->deflicker_target; }
void llrpSetDeflickerTarget(mlvObject_t * video, int value) { video->llrawproc->deflicker_target = value; }
int llrpGetDualIsoMode(mlvObject_t * video) { return video->llrawproc->dual_iso; }
void llrpSetDualIsoMode(mlvObject_t * video, int value) { video->llrawproc->dual_iso = value; }
int llrpGetDualIsoInterpolationMethod(mlvObject_t * video) { return video->llrawproc->diso_averaging; }
void llrpSetDualIsoInterpolationMethod(mlvObject_t * video, int value) { video->llrawproc->diso_averaging = value; }
int llrpGetDualIsoPlaybackForceMean23(mlvObject_t * video) { return video->llrawproc->diso_playback_force_mean23; }
void llrpSetDualIsoPlaybackForceMean23(mlvObject_t * video, int value) { video->llrawproc->diso_playback_force_mean23 = value ? 1 : 0; }
int llrpGetDualIsoAliasMapMode(mlvObject_t * video) { return video->llrawproc->diso_alias_map; }
void llrpSetDualIsoAliasMapMode(mlvObject_t * video, int value) { video->llrawproc->diso_alias_map = value; }
int llrpGetDualIsoFullResBlendingMode(mlvObject_t * video) { return video->llrawproc->diso_frblending; }
void llrpSetDualIsoFullResBlendingMode(mlvObject_t * video, int value) { video->llrawproc->diso_frblending = value; }
int llrpGetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video) { return video->llrawproc->diso_playback_force_disable_alias_map; }
void llrpSetDualIsoPlaybackForceDisableAliasMap(mlvObject_t * video, int value) { video->llrawproc->diso_playback_force_disable_alias_map = value ? 1 : 0; }
int llrpGetDualIsoValidity(mlvObject_t * video) { return video->llrawproc->diso_validity; }
void llrpSetDualIsoValidity(mlvObject_t * video, int diso_force)
{
    video->llrawproc->diso_validity = diso_force ? DISO_FORCED : DISO_INVALID;
}
int llrpHQDualIso(mlvObject_t * video)
{
    return (video->llrawproc->dual_iso == 1) && video->llrawproc->diso_validity && video->llrawproc->fix_raw;
}
void llrpResetDngBWLevels(mlvObject_t * video)
{
    video->llrawproc->dng_black_level = getMlvBlackLevel(video);
    video->llrawproc->dng_white_level = getMlvWhiteLevel(video);
    video->llrawproc->dng_bit_depth = getMlvBitdepth(video);
}
void llrpResetFpmStatus(mlvObject_t * video) { video->llrawproc->fpm_status = 0; }
void llrpResetBpmStatus(mlvObject_t * video) { video->llrawproc->bpm_status = 0; }
int llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread(int) { return 0; }
void llrpInitDarkFrameExtFileName(mlvObject_t * video, char * df_filename)
{
    if (video->llrawproc->dark_frame_filename) {
        std::free(video->llrawproc->dark_frame_filename);
    }
    const std::size_t length = std::strlen(df_filename) + 1;
    video->llrawproc->dark_frame_filename = static_cast<char *>(std::malloc(length));
    std::memcpy(video->llrawproc->dark_frame_filename, df_filename, length);
}
void llrpFreeDarkFrameExtFileName(mlvObject_t * video)
{
    if (video->llrawproc->dark_frame_filename) {
        std::free(video->llrawproc->dark_frame_filename);
        video->llrawproc->dark_frame_filename = nullptr;
    }
}
int llrpGetDarkFrameMode(mlvObject_t * video) { return video->llrawproc->dark_frame; }
void llrpSetDarkFrameMode(mlvObject_t * video, int value) { video->llrawproc->dark_frame = value; }
int llrpGetDarkFrameExtStatus(mlvObject_t * video) { return video->llrawproc->dark_frame_filename ? 1 : 0; }
int llrpGetDarkFrameIntStatus(mlvObject_t *) { return 0; }
int llrpValidateExtDarkFrame(mlvObject_t *, char *, char * error_message)
{
    if (error_message) {
        error_message[0] = '\0';
    }
    return 0;
}

void processingSetTransformation(processingObject_t * processing, int transformation)
{
    processing->transformation = static_cast<uint8_t>(transformation);
}

void processingSetBlackAndWhiteLevel(processingObject_t * processing,
                                     float mlvBlackLevel,
                                     int mlvWhiteLevel,
                                     int mlvBitDepth)
{
    const int bits_shift = 16 - mlvBitDepth;
    if (mlvBlackLevel >= 0) {
        processing->black_level = mlvBlackLevel * (1 << bits_shift);
    }
    if (mlvWhiteLevel >= 0) {
        processing->white_level = static_cast<int>((mlvWhiteLevel << bits_shift) * 0.993);
    }
}

void processingSetBlackLevel(processingObject_t * processing, float mlvBlackLevel, int mlvBitDepth)
{
    processingSetBlackAndWhiteLevel(processing, mlvBlackLevel, -1, mlvBitDepth);
}

void processingSetWhiteLevel(processingObject_t * processing, int mlvWhiteLevel, int mlvBitDepth)
{
    processingSetBlackAndWhiteLevel(processing, -1, mlvWhiteLevel, mlvBitDepth);
}

void processingSetWhiteBalance(processingObject_t * processing, double WBKelvin, double WBTint)
{
    if (!processing) {
        return;
    }
    processing->kelvin = WBKelvin;
    processing->wb_tint = WBTint;
    processing->wb_multipliers[0] = 1.0;
    processing->wb_multipliers[1] = 1.0;
    processing->wb_multipliers[2] = 1.0;
}

processingObject_t * processingCloneForAnalysis(const processingObject_t *)
{
    return nullptr; /* ReceiptApplier::processedThumbnailAtExposure() then reports "not rendered" */
}

void processingFreeClone(processingObject_t *) {}

void processingSetExposureStops(processingObject_t * processing, double exposureStops)
{
    if (processing) {
        processing->exposure_stops = exposureStops;
    }
}

int processingWhiteBalanceControlsForAsShotNeutral(const double[3], int *, int *)
{
    return 0; /* console tests never open a clip; only WB_CUSTOM DNG sequences reach the fit (pipeline test covers it) */
}

void getMlvRawFrameFloat(mlvObject_t *, uint64_t, float *) {}
int getMlvRawFrameProcessedUint16(mlvObject_t *, uint64_t, uint16_t *, int * bit_shift)
{
    if (bit_shift) {
        *bit_shift = 0;
    }
    return 0;
}

int getMlvRawFrameProcessedUint16Direct(mlvObject_t *, uint64_t, uint16_t *, int * bit_shift)
{
    if (bit_shift) {
        *bit_shift = 0;
    }
    return 0;
}

static void fill_stub_thumbnail(mlvObject_t * video, int downscale_factor, unsigned char * out_buffer)
{
    if (!out_buffer) {
        return;
    }
    const int divisor = downscale_factor > 0 ? downscale_factor : 1;
    const int width = video && video->RAWI.xRes > 0 ? video->RAWI.xRes / divisor : 1;
    const int height = video && video->RAWI.yRes > 0 ? video->RAWI.yRes / divisor : 1;
    std::memset(out_buffer, 128, static_cast<std::size_t>(width * height * 3));
}

void get_area_average_downscale_raw_thumnail(mlvObject_t * video, int, int downscale_factor, unsigned char * out_buffer)
{
    fill_stub_thumbnail(video, downscale_factor, out_buffer);
}

void get_area_average_downscale_thumnail(mlvObject_t * video, int, int downscale_factor, int, unsigned char * out_buffer)
{
    fill_stub_thumbnail(video, downscale_factor, out_buffer);
}

int get_area_average_downscale_thumnail_with_processing(mlvObject_t * video, int, int downscale_factor, int,
                                                        processingObject_t *, const mlv_processed_thumbnail_settings_t *,
                                                        unsigned char * out_buffer)
{
    fill_stub_thumbnail(video, downscale_factor, out_buffer);
    return 1;
}

int get_area_average_downscale_thumnail_with_processing_cachefree(mlvObject_t * video, int, int downscale_factor, int,
                                                                  processingObject_t *, const mlv_processed_thumbnail_settings_t *,
                                                                  unsigned char * out_buffer)
{
    fill_stub_thumbnail(video, downscale_factor, out_buffer);
    return 1;
}

void findMlvWhiteBalance(mlvObject_t *, uint64_t, int, int, int * wbTemp, int * wbTint, int)
{
    if (wbTemp) {
        *wbTemp = 6000;
    }
    if (wbTint) {
        *wbTint = 0;
    }
}

void findMlvWhiteBalanceIsolated(mlvObject_t *, uint64_t, int, int, int * wbTemp, int * wbTint, int)
{
    if (wbTemp) {
        *wbTemp = 6000;
    }
    if (wbTint) {
        *wbTint = 0;
    }
}

void wb_convert(wb_convert_info_t * wb_info, float *, int, int, int)
{
    if (wb_info) {
        wb_info->r = 1.0f;
        wb_info->g = 1.0f;
        wb_info->b = 1.0f;
    }
}

void wb_undo(const wb_convert_info_t *, uint16_t *, int, int, int) {}

void debayerLibRtProcess(uint16_t *, float *, int, int, int, double[9]) {}
int debayerAmaze(uint16_t *, float *, int, int, int, int) { return 1; }
void debayerEasy(uint16_t *, float *, int, int, int, int) {}
void debayerNoneU16(uint16_t *, const uint16_t *, int, int, int, int) {}
void debayerBasicU16(uint16_t *, uint16_t *, int, int, int, int) {}
int debayerBasicU16Avx2Active(void) { return 0; }
void debayerAhd(uint16_t *, float *, int, int) {}
void debayerBasic(uint16_t *, float *, int, int, int) {}

int getMlvLastProcessed8DirectPathActive(void) { return 0; }

/*
 * console_tests links frame_caching.c without the production video_mlv.c
 * implementation. Mirror only the synchronous current/slot bookkeeping that
 * invalidateMlvProcessedPreviewCache exposes to these tests. The async mutex,
 * generation, request, worker, and broadcast contract remains covered by the
 * pipeline tests that link video_mlv.c.
 */
void mlvInvalidateProcessed8PrefetchCache(mlvObject_t * video)
{
    if (!video) {
        return;
    }
    video->current_processed_frame_8bit_active = 0;
    video->current_processed_frame_8bit_signature = 0;
    video->current_processed_frame_8bit = 0;
    video->current_processed_frame_8bit_threads = 0;
    video->processed_8bit_cache_next_slot = 0;
    std::memset(video->processed_8bit_cache_active, 0, sizeof(video->processed_8bit_cache_active));
    std::memset(video->processed_8bit_cache_frame, 0, sizeof(video->processed_8bit_cache_frame));
    std::memset(video->processed_8bit_cache_threads, 0, sizeof(video->processed_8bit_cache_threads));
    std::memset(video->processed_8bit_cache_signature, 0, sizeof(video->processed_8bit_cache_signature));
    std::memset(video->processed_8bit_cache_scale, 0, sizeof(video->processed_8bit_cache_scale));
    std::memset(video->processed_8bit_cache_phase4b_path, 0, sizeof(video->processed_8bit_cache_phase4b_path));
    std::memset(video->processed_8bit_cache_phase4b_y_crop_rows, 0, sizeof(video->processed_8bit_cache_phase4b_y_crop_rows));
    std::memset(video->processed_8bit_cache_state, 0, sizeof(video->processed_8bit_cache_state));
    std::memset(video->processed_8bit_cache_prefetched, 0, sizeof(video->processed_8bit_cache_prefetched));
    std::memset(video->processed_8bit_cache_generation, 0, sizeof(video->processed_8bit_cache_generation));
    video->processed_8bit_cache_unit_size = 0;
    video->processed8_prefetch_snapshot_dirty = 1;
}

int demosaic(amazeinfo_t *) { return 1; }

void lrtpCaCorrect(float **, int, int, int, int, const uint8_t, size_t, const double, const double, uint8_t) {}

} // extern "C"
