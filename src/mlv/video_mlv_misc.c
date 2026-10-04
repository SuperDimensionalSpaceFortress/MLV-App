#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "llrawproc/llrawproc.h"
#include "../debayer/debayer.h"
#include "mlv_object.h"
#include "video_mlv.h"

static int look_assist_thumb_trace_enabled(void)
{
    const char * env = getenv("MLVAPP_LOOK_ASSIST_THUMB_TRACE");
    return env && *env && strcmp(env, "0") != 0;
}

static uint64_t fnv1a64_bytes(const void * data, size_t bytes)
{
    const unsigned char * p = (const unsigned char *)data;
    uint64_t hash = 1469598103934665603ULL;

    for (size_t i = 0; i < bytes; ++i)
    {
        hash ^= (uint64_t)p[i];
        hash *= 1099511628211ULL;
    }

    return hash;
}

static void trace_look_assist_thumbnail(const char * kind,
                                        int frame_index,
                                        int raw_w,
                                        int raw_h,
                                        int thumb_w,
                                        int thumb_h,
                                        int downscale_factor,
                                        uint64_t hash)
{
    if (!look_assist_thumb_trace_enabled()) return;

    fprintf(stderr,
            "THUMB_TRACE kind=%s frame=%d raw=%dx%d thumb=%dx%d factor=%d hash=%016llx\n",
            kind ? kind : "unknown",
            frame_index,
            raw_w,
            raw_h,
            thumb_w,
            thumb_h,
            downscale_factor,
            (unsigned long long)hash);
}

static void downscale_rgb16_average_to_rgb16(const uint16_t * input,
                                             int input_w,
                                             int downscale_factor,
                                             int thumb_w,
                                             int thumb_h,
                                             uint16_t * output)
{
    uint64_t denominator = (uint64_t)downscale_factor * (uint64_t)downscale_factor;

    for (int outY = 0; outY < thumb_h; ++outY) {
        for (int outX = 0; outX < thumb_w; ++outX) {
            uint64_t sum_r = 0;
            uint64_t sum_g = 0;
            uint64_t sum_b = 0;

            int start_y = outY * downscale_factor;
            int start_x = outX * downscale_factor;

            for (int j = 0; j < downscale_factor; j++) {
                for (int i = 0; i < downscale_factor; i++) {
                    size_t pixel_index = ((size_t)(start_y + j) * input_w + (start_x + i)) * 3u;
                    sum_r += input[pixel_index + 0];
                    sum_g += input[pixel_index + 1];
                    sum_b += input[pixel_index + 2];
                }
            }

            size_t out_pixel_index = ((size_t)outY * thumb_w + outX) * 3u;
            output[out_pixel_index + 0] = (uint16_t)(sum_r / denominator);
            output[out_pixel_index + 1] = (uint16_t)(sum_g / denominator);
            output[out_pixel_index + 2] = (uint16_t)(sum_b / denominator);
        }
    }
}

static void downscale_rgb16_average_to_rgb8(const uint16_t * input,
                                            int input_w,
                                            int downscale_factor,
                                            int thumb_w,
                                            int thumb_h,
                                            unsigned char * output)
{
    uint64_t denominator = (uint64_t)downscale_factor * (uint64_t)downscale_factor;

    for (int outY = 0; outY < thumb_h; ++outY) {
        for (int outX = 0; outX < thumb_w; ++outX) {
            uint64_t sum_r = 0;
            uint64_t sum_g = 0;
            uint64_t sum_b = 0;

            int start_y = outY * downscale_factor;
            int start_x = outX * downscale_factor;

            for (int j = 0; j < downscale_factor; j++) {
                for (int i = 0; i < downscale_factor; i++) {
                    size_t pixel_index = ((size_t)(start_y + j) * input_w + (start_x + i)) * 3u;
                    sum_r += input[pixel_index + 0];
                    sum_g += input[pixel_index + 1];
                    sum_b += input[pixel_index + 2];
                }
            }

            size_t out_pixel_index = ((size_t)outY * thumb_w + outX) * 3u;
            output[out_pixel_index + 0] = (unsigned char)((sum_r / denominator) >> 8);
            output[out_pixel_index + 1] = (unsigned char)((sum_g / denominator) >> 8);
            output[out_pixel_index + 2] = (unsigned char)((sum_b / denominator) >> 8);
        }
    }
}

static void apply_processed_thumbnail_settings(
    processingObject_t *processing,
    const mlv_processed_thumbnail_settings_t *settings)
{
    if (!processing || !settings) {
        return;
    }

    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_RAW_LEVELS) {
        if (settings->raw_bit_depth > 0) {
            processingSetBlackAndWhiteLevel(processing,
                                            settings->raw_black_level,
                                            settings->raw_white_level,
                                            settings->raw_bit_depth);
        }
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_WHITE_BALANCE) {
        processingSetWhiteBalance(processing,
                                  settings->white_balance_kelvin,
                                  settings->white_balance_tint);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE) {
        processingSetExposureStops(processing, settings->exposure_stops);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_SIMPLE_CONTRAST) {
        processingSetSimpleContrast(processing, settings->simple_contrast);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_PIVOT) {
        processingSetPivot(processing, settings->pivot);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_SHADOWS) {
        processingSetShadows(processing, settings->shadows);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_HIGHLIGHTS) {
        processingSetHighlights(processing, settings->highlights);
    }
    if (settings->flags & MLV_PROCESSED_THUMBNAIL_APPLY_VIBRANCE) {
        processingSetVibrance(processing, settings->vibrance);
    }
}

static int render_downscaled_processed_thumbnail_from_rgb16(
    int frame_index,
    int raw_w,
    int raw_h,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    const uint16_t *debayered_raw_frame,
    unsigned char *out_buffer)
{
    const int thumbW = raw_w / downscale_factor;
    const int thumbH = raw_h / downscale_factor;
    if (thumbW <= 0 || thumbH <= 0) {
        return 0;
    }

    uint16_t *downscaled_image = (uint16_t *) malloc(
        (size_t)thumbW * (size_t)thumbH * 3u * sizeof(uint16_t));
    if (!downscaled_image) {
        return 0;
    }

    downscale_rgb16_average_to_rgb16(debayered_raw_frame, raw_w, downscale_factor,
                                     thumbW, thumbH, downscaled_image);
    trace_look_assist_thumbnail("processed-input16", frame_index, raw_w, raw_h, thumbW, thumbH,
                                downscale_factor, fnv1a64_bytes(downscaled_image,
                                                               (size_t)thumbW * (size_t)thumbH * 3u * sizeof(uint16_t)));

    uint16_t *downscaled_processed_image = (uint16_t *) malloc(
        (size_t)thumbW * (size_t)thumbH * 3u * sizeof(uint16_t));
    if (!downscaled_processed_image) {
        free(downscaled_image);
        return 0;
    }

    apply_processed_thumbnail_settings(analysis_processing, settings);
    applyProcessingObject(analysis_processing,
                          thumbW, thumbH,
                          downscaled_image,
                          downscaled_processed_image,
                          cpu_cores, 1, frame_index);
    trace_look_assist_thumbnail("processed-output16", frame_index, raw_w, raw_h, thumbW, thumbH,
                                downscale_factor, fnv1a64_bytes(downscaled_processed_image,
                                                               (size_t)thumbW * (size_t)thumbH * 3u * sizeof(uint16_t)));

    size_t size = (size_t)thumbW * (size_t)thumbH * 3u;
    for (size_t i = 0; i < size; i++) {
        out_buffer[i] = downscaled_processed_image[i] >> 8;
    }
    trace_look_assist_thumbnail("processed", frame_index, raw_w, raw_h, thumbW, thumbH,
                                downscale_factor, fnv1a64_bytes(out_buffer, size));

    free(downscaled_processed_image);
    free(downscaled_image);
    return 1;
}

static int try_render_cachefree_scaled_processed_thumbnail(
    mlvObject_t *video,
    int frame_index,
    int raw_w,
    int raw_h,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    unsigned char *out_buffer)
{
    const int candidate_scales[] = { 8, 4, 2 };
    const size_t candidate_count = sizeof(candidate_scales) / sizeof(candidate_scales[0]);

    for (size_t i = 0; i < candidate_count; ++i) {
        const int source_scale = candidate_scales[i];
        if (downscale_factor % source_scale != 0) {
            continue;
        }

        const int expected_w = raw_w / source_scale;
        const int expected_h = raw_h / source_scale;
        const int source_downscale = downscale_factor / source_scale;
        if (expected_w <= 0 || expected_h <= 0 || source_downscale <= 0) {
            continue;
        }

        uint16_t *scaled_rgb = (uint16_t *) malloc(
            (size_t)expected_w * (size_t)expected_h * 3u * sizeof(uint16_t));
        if (!scaled_rgb) {
            return 0;
        }

        int actual_w = 0;
        int actual_h = 0;
        int actual_scale = 0;
        const int got_scaled =
            get_mlv_raw_frame_debayered_isolated_analysis_scaled(video,
                                                                 frame_index,
                                                                 scaled_rgb,
                                                                 source_scale,
                                                                 cpu_cores,
                                                                 &actual_w,
                                                                 &actual_h,
                                                                 &actual_scale);
        if (got_scaled
            && actual_scale == source_scale
            && actual_w == expected_w
            && actual_h == expected_h) {
            trace_look_assist_thumbnail("processed-source-cachefree-scaled",
                                        frame_index, raw_w, raw_h, actual_w, actual_h,
                                        actual_scale, fnv1a64_bytes(scaled_rgb,
                                                                    (size_t)actual_w * (size_t)actual_h * 3u * sizeof(uint16_t)));
            mlvSetAnalysisProcessingClipLevels(video, analysis_processing);

            const int result =
                render_downscaled_processed_thumbnail_from_rgb16(frame_index,
                                                                 actual_w,
                                                                 actual_h,
                                                                 source_downscale,
                                                                 cpu_cores,
                                                                 analysis_processing,
                                                                 settings,
                                                                 scaled_rgb,
                                                                 out_buffer);
            free(scaled_rgb);
            if (result) {
                return 1;
            }
        }
        else {
            free(scaled_rgb);
        }
    }

    return 0;
}

int create_thumbnail(mlvObject_t * video, uint8_t * thumbnail_img, int downscaled_factor, int width, int height, int threads)
{
    int raw_w = video->RAWI.xRes;
    int raw_h = video->RAWI.yRes;
    int i, j;

    uint16_t *raw_frame = (uint16_t *)malloc((size_t)raw_w * (size_t)raw_h * sizeof(uint16_t));
    if (getMlvRawFrameUint16(video, 0, raw_frame))
    {
        free(raw_frame);
        return 1;
    }

    int pixel_count = (width) * (height);

    uint16_t *downscaled_frame = (uint16_t *)(malloc(pixel_count * sizeof(uint16_t)));

    if (!downscaled_frame)
    {
        free(raw_frame);
        return 1;
    }

    for (i = 0; i < height; i++)
        for (j = 0; j < width; j++)
            downscaled_frame[i * width + j] = raw_frame[(i * downscaled_factor) * raw_w + (j * downscaled_factor)];

    int shift_val = (llrpHQDualIso(video)) ? 0 : (16 - video->RAWI.raw_info.bits_per_pixel);

    float *float_thumb = (float *)(malloc(pixel_count * sizeof(float)));

    if (!float_thumb)
    {
        free(raw_frame);
        free(downscaled_frame);
        return 1;
    }

    for (i = 0; i < pixel_count; i++)
        float_thumb[i] = (float)(downscaled_frame[i] << shift_val);

    uint16_t *debayered_frame = (uint16_t *)(malloc(pixel_count * 3 * sizeof(uint16_t)));

    if (!debayered_frame)
    {
        free(raw_frame);
        free(downscaled_frame);
        free(float_thumb);
        return 1;
    }

    debayerBasic(debayered_frame, float_thumb, width, height, 1);

    uint16_t *processed_frame = (uint16_t *)(malloc(pixel_count * 3 * sizeof(uint16_t)));

    if (!processed_frame)
    {
        free(raw_frame);
        free(downscaled_frame);
        free(debayered_frame);
        free(float_thumb);
        return 1;
    }

    applyProcessingObject(video->processing,
                          width, height,
                          debayered_frame,
                          processed_frame,
                          threads, 1, 0);

    for (i = 0; i < pixel_count * 3; i++)
        thumbnail_img[i] = (uint8_t)(processed_frame[i] >> 8);

    free(raw_frame);
    free(downscaled_frame);
    free(debayered_frame);
    free(float_thumb);
    free(processed_frame);

    return 0;
}

int get_area_average_downscale_thumnail_with_processing(
    mlvObject_t *video,
    int frame_index,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    unsigned char *out_buffer)
{
    if (!video || !analysis_processing || !out_buffer) {
        return 0;
    }

    /* Get RAW frame info */
    int raw_w = video->RAWI.xRes;
    int raw_h = video->RAWI.yRes;

    if (raw_w <= 0 || raw_h <= 0 || downscale_factor <= 0) {
        return 0;
    }

    float *raw_frame = (float *) malloc((size_t)raw_w * (size_t)raw_h * sizeof(float));
    if (!raw_frame) {
        return 0;
    }

    getMlvRawFrameFloat(video, frame_index, raw_frame);
    mlvSetAnalysisProcessingClipLevels(video, analysis_processing);

    uint16_t *debayered_raw_frame = (uint16_t *) malloc(
        (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t));
    if (!debayered_raw_frame) {
        free(raw_frame);
        return 0;
    }

    debayerBasic(debayered_raw_frame, raw_frame, raw_w, raw_h, 1);
    trace_look_assist_thumbnail("processed-source-live", frame_index, raw_w, raw_h, raw_w, raw_h,
                                1, fnv1a64_bytes(debayered_raw_frame,
                                                 (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t)));

    int result = render_downscaled_processed_thumbnail_from_rgb16(frame_index,
                                                                  raw_w,
                                                                  raw_h,
                                                                  downscale_factor,
                                                                  cpu_cores,
                                                                  analysis_processing,
                                                                  settings,
                                                                  debayered_raw_frame,
                                                                  out_buffer);

    /* Cleanup */
    free(debayered_raw_frame);
    free(raw_frame);
    return result;
}

int get_area_average_downscale_thumnail_with_processing_cachefree(
    mlvObject_t *video,
    int frame_index,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    unsigned char *out_buffer)
{
    if (!video || !analysis_processing || !out_buffer) {
        return 0;
    }

    int raw_w = video->RAWI.xRes;
    int raw_h = video->RAWI.yRes;

    if (raw_w <= 0 || raw_h <= 0 || downscale_factor <= 0) {
        return 0;
    }

    const int thumbW = raw_w / downscale_factor;
    const int thumbH = raw_h / downscale_factor;
    if (thumbW <= 0 || thumbH <= 0) {
        return 0;
    }

    if (try_render_cachefree_scaled_processed_thumbnail(video,
                                                        frame_index,
                                                        raw_w,
                                                        raw_h,
                                                        downscale_factor,
                                                        cpu_cores,
                                                        analysis_processing,
                                                        settings,
                                                        out_buffer)) {
        return 1;
    }

    float *temp_frame = (float *) malloc((size_t)raw_w * (size_t)raw_h * sizeof(float));
    if (!temp_frame) {
        return 0;
    }

    uint16_t *debayered_raw_frame = (uint16_t *) malloc(
        (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t));
    if (!debayered_raw_frame) {
        free(temp_frame);
        return 0;
    }

    get_mlv_raw_frame_debayered_isolated_analysis(video,
                                                  frame_index,
                                                  temp_frame,
                                                  debayered_raw_frame,
                                                  0);
    mlvSetAnalysisProcessingClipLevels(video, analysis_processing);
    trace_look_assist_thumbnail("processed-source-cachefree", frame_index, raw_w, raw_h, raw_w, raw_h,
                                1, fnv1a64_bytes(debayered_raw_frame,
                                                 (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t)));

    int result = render_downscaled_processed_thumbnail_from_rgb16(frame_index,
                                                                  raw_w,
                                                                  raw_h,
                                                                  downscale_factor,
                                                                  cpu_cores,
                                                                  analysis_processing,
                                                                  settings,
                                                                  debayered_raw_frame,
                                                                  out_buffer);

    free(debayered_raw_frame);
    free(temp_frame);
    return result;
}

void get_area_average_downscale_thumnail(mlvObject_t *video, int frame_index, int downscale_factor, int cpu_cores, unsigned char *out_buffer)
{
    if (!video || !out_buffer) {
        return;
    }

    processingObject_t *analysis_processing = processingCloneForAnalysis(video->processing);
    if (!analysis_processing) {
        return;
    }

    get_area_average_downscale_thumnail_with_processing(video,
                                                        frame_index,
                                                        downscale_factor,
                                                        cpu_cores,
                                                        analysis_processing,
                                                        NULL,
                                                        out_buffer);
    processingFreeClone(analysis_processing);
}

void get_area_average_downscale_raw_thumnail(mlvObject_t *video, int frame_index, int downscale_factor, unsigned char *out_buffer)
{
    if (!video || !out_buffer) {
        return;
    }

    int raw_w = video->RAWI.xRes;
    int raw_h = video->RAWI.yRes;

    if (raw_w <= 0 || raw_h <= 0 || downscale_factor <= 0) {
        return;
    }

    const int thumbW = raw_w / downscale_factor;
    const int thumbH = raw_h / downscale_factor;
    if (thumbW <= 0 || thumbH <= 0) {
        return;
    }

    float *raw_frame = (float *) malloc((size_t)raw_w * (size_t)raw_h * sizeof(float));
    if (!raw_frame) {
        return;
    }

    getMlvRawFrameFloat(video, frame_index, raw_frame);

    uint16_t *debayered_raw_frame = (uint16_t *) malloc(
        (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t));
    if (!debayered_raw_frame) {
        free(raw_frame);
        return;
    }

    debayerBasic(debayered_raw_frame, raw_frame, raw_w, raw_h, 1);
    trace_look_assist_thumbnail("raw-source-live", frame_index, raw_w, raw_h, raw_w, raw_h,
                                1, fnv1a64_bytes(debayered_raw_frame,
                                                 (size_t)raw_w * (size_t)raw_h * 3u * sizeof(uint16_t)));

    size_t size = (size_t)thumbW * (size_t)thumbH * 3u;
    downscale_rgb16_average_to_rgb8(debayered_raw_frame, raw_w, downscale_factor,
                                    thumbW, thumbH, out_buffer);
    trace_look_assist_thumbnail("raw", frame_index, raw_w, raw_h, thumbW, thumbH,
                                downscale_factor, fnv1a64_bytes(out_buffer, size));

    free(debayered_raw_frame);
    free(raw_frame);
}
