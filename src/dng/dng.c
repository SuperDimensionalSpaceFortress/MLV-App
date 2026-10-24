/*
 * Copyright (C) 2014 David Milligan
 *
 * Updated and modified by bouncyball (2016-2017)
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
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <limits.h>
#include <errno.h>
#include <pthread.h>
#include <time.h>

#if defined(_WIN32) || defined(__WIN32)
#include <windows.h>
#endif

#include "dng.h"
#include "dng_tag_codes.h"
#include "dng_tag_types.h"
#include "dng_tag_values.h"
#include "../mlv/camid/camera_id.h"
#include "../mlv/video_mlv.h"

#include "../mlv/liblj92/lj92.h"
#include "../mlv/llrawproc/llrawproc.h"
#include "../mlv/mcraw/mcraw.h"
#include "../mlv/macros.h"

#define IFD0_COUNT 41
#define EXIF_IFD_COUNT 11
#define PACK(a) (((uint16_t)a[1] << 16) | ((uint16_t)a[0]))
#define PACK2(a,b) (((uint16_t)b << 16) | ((uint16_t)a))
#define STRING_ENTRY(a,b,c) (uint32_t)(strlen(a) + 1), add_string(a, b, c)
#define RATIONAL_ENTRY(a,b,c,d) (d/2), add_array(a, b, c, d)
#define RATIONAL_ENTRY2(a,b,c,d) 1, add_rational(a, b, c, d)
#define ARRAY_ENTRY(a,b,c,d) d, add_array(a, b, c, d)
#define HEADER_SIZE 1536
#define COUNT(x) ((int)(sizeof(x)/sizeof((x)[0])))

#define SOFTWARE_NAME "MLV App"
#define MIN(a,b) (((a)<(b))?(a):(b))
#define MAX(a,b) (((a)>(b))?(a):(b))
#define COERCE(x,lo,hi) MAX(MIN((x),(hi)),(lo))
#define ABS(a) ((a) > 0 ? (a) : -(a))
#define ROR16(v,a) ((v) >> (a) | (v) << (16-(a)))
#define ROL16(v,a) ((v) << (a) | (v) >> (16-(a)))
#define log2(x) log((float)(x))/log(2.)

#ifndef MLVAPP_GIT_SHA
#define MLVAPP_GIT_SHA "unknown"
#endif

#ifdef __WIN32
#define FMT_SIZE "%u"
#else
#define FMT_SIZE "%zu"
#endif

#define DNG_PAYLOAD_WRITER_DEFAULT_QUEUE_DEPTH 1
#define DNG_PAYLOAD_WRITER_MAX_QUEUE_DEPTH 8
#define DNG_PAYLOAD_WRITER_DEFAULT_THREAD_COUNT 1
#define DNG_PAYLOAD_WRITER_MAX_THREAD_COUNT 4
#define DNG_PAYLOAD_WRITER_MAX_DEBUG_DELAY_MS 60000
#define DNG_ASYNC_WRITER_COMPRESS_ENV "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS"
#define DNG_COMPRESS_PLACEMENT_PRODUCER "producer_before_payload"
#define DNG_COMPRESS_PLACEMENT_WRITER "async_writer_after_payload"

static uint64_t file_set_pos(FILE *stream, uint64_t offset, int whence)
{
#if defined(__WIN32)
    return fseeko64(stream, offset, whence);
#else
    return fseek(stream, offset, whence);
#endif
}

enum { IMG_SIZE_UNPACKED, IMG_SIZE_PACKED, IMG_SIZE_LOSLESS };

static void apply_llrawproc_locked(mlvObject_t * mlv_data,
                                   uint16_t * image_buf_unpacked,
                                   uint64_t image_size_unpacked)
{
    applyLLRawProcObject(mlv_data, image_buf_unpacked, image_size_unpacked);
}

typedef enum
{
    EXPORT_PROFILE_RAW_READ,
    EXPORT_PROFILE_RAW_DECODE,
    EXPORT_PROFILE_RAW_UNPACK,
    EXPORT_PROFILE_LLRAWPROC,
    EXPORT_PROFILE_LLRAWPROC_TOTAL,
    EXPORT_PROFILE_LLRAWPROC_DARK_FRAME,
    EXPORT_PROFILE_LLRAWPROC_VERTICAL_STRIPES,
    EXPORT_PROFILE_LLRAWPROC_FOCUS_PIXELS,
    EXPORT_PROFILE_LLRAWPROC_BAD_PIXELS,
    EXPORT_PROFILE_LLRAWPROC_PATTERN_NOISE,
    EXPORT_PROFILE_LLRAWPROC_PRE_DUALISO_FIX,
    EXPORT_PROFILE_LLRAWPROC_DUAL_ISO,
    EXPORT_PROFILE_LLRAWPROC_CHROMA_SMOOTH,
    EXPORT_PROFILE_LLRAWPROC_SHARED_LOCK,
    EXPORT_PROFILE_LLRAWPROC_DUALISO_REFINE_LOCK,
    EXPORT_PROFILE_LLRAWPROC_PUBLISH_LOCK,
    EXPORT_PROFILE_LLRAWPROC_OTHER,
    EXPORT_PROFILE_DNG_HEADER,
    EXPORT_PROFILE_DNG_PACK,
    EXPORT_PROFILE_DNG_COMPRESS,
    EXPORT_PROFILE_DNG_COMPRESS_ENCODE,
    EXPORT_PROFILE_DNG_COMPRESS_COPY,
    EXPORT_PROFILE_DNG_COMPRESS_CLEANUP,
    EXPORT_PROFILE_PAYLOAD_CLONE,
    EXPORT_PROFILE_DISK_WRITE,
    EXPORT_PROFILE_WRITER_QUEUE_WAIT,
    EXPORT_PROFILE_QUEUE_IDLE,
    EXPORT_PROFILE_PRODUCER_QUEUE_IDLE,
    EXPORT_PROFILE_PRODUCER_FRAME,
    EXPORT_PROFILE_FRAME_TOTAL,
    EXPORT_PROFILE_STAGE_COUNT
} exportProfileStage_t;

#define EXPORT_PROFILE_PATH_MAX 1024
#define EXPORT_PROFILE_BUILD_ID_MAX 128

typedef struct
{
    int active;
    uint32_t frame_index;
    int raw_input_state;
    int raw_output_state;
    int success;
    char clip_path[EXPORT_PROFILE_PATH_MAX];
    double stages_ms[EXPORT_PROFILE_STAGE_COUNT];
    uint32_t stage_mask;
    int gpu_export_attempted;
    int gpu_export_rc;
    int gpu_export_replaced;
    int gpu_export_trusted;
    int gpu_export_allocated_bytes_valid;
    uint64_t gpu_export_allocated_bytes;
    int gpu_export_skip_code;
    int dng_compress_bytes_valid;
    uint64_t dng_compress_input_bytes;
    uint64_t dng_compress_output_bytes;
} exportProfileFrame_t;

static exportProfileFrame_t * g_export_profile_frames = NULL;
static size_t g_export_profile_frame_count = 0;
static size_t g_export_profile_frame_capacity = 0;
static char g_export_profile_path[EXPORT_PROFILE_PATH_MAX] = "";
static char g_export_profile_build_id[EXPORT_PROFILE_BUILD_ID_MAX] = "";
static int g_export_profile_atexit_registered = 0;
static int g_export_profile_write_in_progress = 0;
static double g_export_profile_last_frame_finish_ms = 0.0;
static double g_export_profile_last_producer_finish_ms = 0.0;
static unsigned g_export_profile_async_writer_thread_count = 0;
static unsigned g_export_profile_async_writer_queue_capacity = 0;
static unsigned g_export_profile_async_writer_max_queued = 0;
static unsigned g_export_profile_async_writer_jobs_started = 0;
static unsigned g_export_profile_async_writer_jobs_finished = 0;
static unsigned g_export_profile_async_writer_max_active = 0;
static int g_export_profile_async_writer_compress_used = 0;
static int g_export_profile_field_log_appended = 0;
static pthread_mutex_t g_export_profile_mutex = PTHREAD_MUTEX_INITIALIZER;

static int export_profile_env_truthy(const char * value)
{
    if(value == NULL || value[0] == '\0') return 0;
    if(strcmp(value, "0") == 0) return 0;
    if(strcmp(value, "false") == 0 || strcmp(value, "FALSE") == 0) return 0;
    if(strcmp(value, "off") == 0 || strcmp(value, "OFF") == 0) return 0;
    if(strcmp(value, "no") == 0 || strcmp(value, "NO") == 0) return 0;
    return 1;
}

static const char * export_profile_raw_state_name(int state)
{
    switch(state)
    {
        case UNCOMPRESSED_RAW:  return "uncompressed_raw";
        case COMPRESSED_RAW:    return "compressed_raw";
        case UNCOMPRESSED_ORIG: return "uncompressed_original";
        case COMPRESSED_ORIG:   return "compressed_original";
        default:                return "unknown";
    }
}

static const char * export_profile_gpu_export_skip_reason(int skip_code)
{
    switch(skip_code)
    {
        case LLRP_GPU_EXPORT_SKIP_NONE:                 return "none";
        case LLRP_GPU_EXPORT_SKIP_DISABLED:             return "disabled";
        case LLRP_GPU_EXPORT_SKIP_NOT_DUAL_ISO:         return "not_dual_iso";
        case LLRP_GPU_EXPORT_SKIP_EMPTY_RAW_IMAGE:      return "empty_raw_image";
        case LLRP_GPU_EXPORT_SKIP_BACKEND_UNAVAILABLE:  return "backend_unavailable";
        case LLRP_GPU_EXPORT_SKIP_INPUT_ALLOC_FAILED:   return "input_alloc_failed";
        case LLRP_GPU_EXPORT_SKIP_PLAYBACK_RECON_USED:  return "playback_recon_used";
        case LLRP_GPU_EXPORT_SKIP_DUAL_ISO_RECON_FAILED:
                                                            return "dual_iso_recon_failed";
        case LLRP_GPU_EXPORT_SKIP_MISSING_INPUT:        return "missing_input";
        case LLRP_GPU_EXPORT_SKIP_MISSING_RECON_STATE:  return "missing_recon_state";
        case LLRP_GPU_EXPORT_SKIP_INVALID_RECON_STATE:  return "invalid_recon_state";
        case LLRP_GPU_EXPORT_SKIP_SIZE_MISMATCH:        return "size_mismatch";
        case LLRP_GPU_EXPORT_SKIP_OUTPUT_ALLOC_FAILED:  return "output_alloc_failed";
        default:                                        return "unknown";
    }
}

static const char * export_profile_stage_name(exportProfileStage_t stage)
{
    switch(stage)
    {
        case EXPORT_PROFILE_RAW_READ:     return "raw_read_ms";
        case EXPORT_PROFILE_RAW_DECODE:   return "raw_decode_ms";
        case EXPORT_PROFILE_RAW_UNPACK:   return "raw_unpack_ms";
        case EXPORT_PROFILE_LLRAWPROC:    return "llrawproc_ms";
        case EXPORT_PROFILE_LLRAWPROC_TOTAL:
                                          return "llrawproc_total_ms";
        case EXPORT_PROFILE_LLRAWPROC_DARK_FRAME:
                                          return "llrawproc_dark_frame_ms";
        case EXPORT_PROFILE_LLRAWPROC_VERTICAL_STRIPES:
                                          return "llrawproc_vertical_stripes_ms";
        case EXPORT_PROFILE_LLRAWPROC_FOCUS_PIXELS:
                                          return "llrawproc_focus_pixels_ms";
        case EXPORT_PROFILE_LLRAWPROC_BAD_PIXELS:
                                          return "llrawproc_bad_pixels_ms";
        case EXPORT_PROFILE_LLRAWPROC_PATTERN_NOISE:
                                          return "llrawproc_pattern_noise_ms";
        case EXPORT_PROFILE_LLRAWPROC_PRE_DUALISO_FIX:
                                          return "llrawproc_pre_dualiso_fix_ms";
        case EXPORT_PROFILE_LLRAWPROC_DUAL_ISO:
                                          return "llrawproc_dual_iso_ms";
        case EXPORT_PROFILE_LLRAWPROC_CHROMA_SMOOTH:
                                          return "llrawproc_chroma_smooth_ms";
        case EXPORT_PROFILE_LLRAWPROC_SHARED_LOCK:
                                          return "llrawproc_shared_lock_ms";
        case EXPORT_PROFILE_LLRAWPROC_DUALISO_REFINE_LOCK:
                                          return "llrawproc_dualiso_refine_lock_ms";
        case EXPORT_PROFILE_LLRAWPROC_PUBLISH_LOCK:
                                          return "llrawproc_publish_lock_ms";
        case EXPORT_PROFILE_LLRAWPROC_OTHER:
                                          return "llrawproc_other_ms";
        case EXPORT_PROFILE_DNG_HEADER:   return "dng_header_ms";
        case EXPORT_PROFILE_DNG_PACK:     return "dng_pack_ms";
        case EXPORT_PROFILE_DNG_COMPRESS: return "dng_compress_ms";
        case EXPORT_PROFILE_DNG_COMPRESS_ENCODE:
                                          return "dng_compress_encode_ms";
        case EXPORT_PROFILE_DNG_COMPRESS_COPY:
                                          return "dng_compress_copy_ms";
        case EXPORT_PROFILE_DNG_COMPRESS_CLEANUP:
                                          return "dng_compress_cleanup_ms";
        case EXPORT_PROFILE_PAYLOAD_CLONE: return "payload_clone_ms";
        case EXPORT_PROFILE_DISK_WRITE:   return "disk_write_ms";
        case EXPORT_PROFILE_WRITER_QUEUE_WAIT:
                                          return "writer_queue_wait_ms";
        case EXPORT_PROFILE_QUEUE_IDLE:   return "queue_idle_ms";
        case EXPORT_PROFILE_PRODUCER_QUEUE_IDLE:
                                          return "producer_queue_idle_ms";
        case EXPORT_PROFILE_PRODUCER_FRAME:
                                          return "producer_frame_ms";
        case EXPORT_PROFILE_FRAME_TOTAL:  return "frame_total_ms";
        default:                          return "unknown_ms";
    }
}

static void export_profile_copy_string(char * dst, size_t dst_size, const char * src)
{
    if(dst_size == 0) return;
    if(src == NULL) src = "";
    strncpy(dst, src, dst_size - 1);
    dst[dst_size - 1] = '\0';
}

static double export_profile_now_ms(void)
{
#if defined(_WIN32) || defined(__WIN32)
    static LARGE_INTEGER freq = { 0 };
    LARGE_INTEGER now;
    if(freq.QuadPart == 0) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return ((double)now.QuadPart * 1000.0) / (double)freq.QuadPart;
#elif defined(CLOCK_MONOTONIC)
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
#else
    return ((double)clock() * 1000.0) / (double)CLOCKS_PER_SEC;
#endif
}

static void export_profile_write_json(void);

static void export_profile_reset_frames(void)
{
    free(g_export_profile_frames);
    g_export_profile_frames = NULL;
    g_export_profile_frame_count = 0;
    g_export_profile_frame_capacity = 0;
    g_export_profile_last_frame_finish_ms = 0.0;
    g_export_profile_last_producer_finish_ms = 0.0;
    g_export_profile_async_writer_thread_count = 0;
    g_export_profile_async_writer_queue_capacity = 0;
    g_export_profile_async_writer_max_queued = 0;
    g_export_profile_async_writer_jobs_started = 0;
    g_export_profile_async_writer_jobs_finished = 0;
    g_export_profile_async_writer_max_active = 0;
    g_export_profile_async_writer_compress_used = 0;
}

void resetDngExportProfilerForTesting(void)
{
    pthread_mutex_lock(&g_export_profile_mutex);
    g_export_profile_path[0] = '\0';
    g_export_profile_build_id[0] = '\0';
    export_profile_reset_frames();
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static void export_profile_note_async_writer_threads(size_t thread_count)
{
    pthread_mutex_lock(&g_export_profile_mutex);
    g_export_profile_async_writer_thread_count = (unsigned)thread_count;
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static void export_profile_note_async_writer_queue(size_t capacity, size_t queued)
{
    pthread_mutex_lock(&g_export_profile_mutex);
    if(capacity > (size_t)g_export_profile_async_writer_queue_capacity)
    {
        g_export_profile_async_writer_queue_capacity = (unsigned)capacity;
    }
    if(queued > (size_t)g_export_profile_async_writer_max_queued)
    {
        g_export_profile_async_writer_max_queued = (unsigned)queued;
    }
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static void export_profile_note_async_writer_activity(size_t active,
                                                      size_t jobs_started,
                                                      size_t jobs_finished)
{
    pthread_mutex_lock(&g_export_profile_mutex);
    if(active > (size_t)g_export_profile_async_writer_max_active)
    {
        g_export_profile_async_writer_max_active = (unsigned)active;
    }
    if(jobs_started > (size_t)g_export_profile_async_writer_jobs_started)
    {
        g_export_profile_async_writer_jobs_started = (unsigned)jobs_started;
    }
    if(jobs_finished > (size_t)g_export_profile_async_writer_jobs_finished)
    {
        g_export_profile_async_writer_jobs_finished = (unsigned)jobs_finished;
    }
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static void export_profile_note_async_writer_compress_used(void)
{
    pthread_mutex_lock(&g_export_profile_mutex);
    g_export_profile_async_writer_compress_used = 1;
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static int dng_async_writer_compress_env_enabled(void)
{
    return export_profile_env_truthy(getenv(DNG_ASYNC_WRITER_COMPRESS_ENV));
}

/* Debug-only millisecond env value, gated on the export profiler (on, profile file set). The
 * value must be decimal digits only: a sign, leading whitespace or trailing characters
 * ("1garbage", " 300", "300 ") read as off, never as a prefix. Capped at
 * DNG_PAYLOAD_WRITER_MAX_DEBUG_DELAY_MS. Returns 0 when off. */
static unsigned dng_payload_writer_debug_ms_from_env(const char * name)
{
    const char * profiler_enabled = getenv("MLVAPP_EXPORT_STAGE_PROFILER");
    const char * profile_file = getenv("MLVAPP_EXPORT_STAGE_PROFILE_FILE");
    const char * value = getenv(name);
    char * end = NULL;
    long parsed = 0;
    if(!export_profile_env_truthy(profiler_enabled) || profile_file == NULL || profile_file[0] == '\0')
    {
        return 0;
    }
    if(value == NULL || value[0] < '0' || value[0] > '9')
    {
        return 0;
    }

    parsed = strtol(value, &end, 10);
    if(end == value || *end != '\0' || parsed <= 0)
    {
        return 0;
    }
    if(parsed > DNG_PAYLOAD_WRITER_MAX_DEBUG_DELAY_MS)
    {
        return DNG_PAYLOAD_WRITER_MAX_DEBUG_DELAY_MS;
    }
    return (unsigned)parsed;
}

static unsigned dng_payload_writer_debug_delay_ms_from_env(void)
{
    return dng_payload_writer_debug_ms_from_env("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_DEBUG_DELAY_MS");
}

static void dng_payload_writer_debug_delay(void)
{
    const unsigned delay_ms = dng_payload_writer_debug_delay_ms_from_env();
    if(delay_ms == 0)
    {
        return;
    }
#if defined(_WIN32) || defined(__WIN32)
    Sleep(delay_ms);
#elif defined(CLOCK_MONOTONIC)
    struct timespec delay;
    delay.tv_sec = (time_t)(delay_ms / 1000u);
    delay.tv_nsec = (long)(delay_ms % 1000u) * 1000000L;
    nanosleep(&delay, NULL);
#else
    const clock_t start = clock();
    const clock_t ticks = (clock_t)(((double)delay_ms / 1000.0) * (double)CLOCKS_PER_SEC);
    while((clock() - start) < ticks) {}
#endif
}

/* Debug-only ordering guarantee for the writer-queue fill test. Same gates and parser as the
 * delay above (profiler on, profile file set) plus
 * MLVAPP_CDNG_EXPORT_ASYNC_WRITER_DEBUG_HOLD_UNTIL_FULL_MS: on its first dequeue only, after the
 * delay above, the writer thread holds until the queue is full (or finish is called), and the
 * value is the upper bound on that wait. Later dequeues get only the delay. Inert when unset.
 * Returns the bound in ms, 0 when the hold is off. */
static unsigned dng_payload_writer_debug_hold_bound_ms_from_env(void)
{
    return dng_payload_writer_debug_ms_from_env("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_DEBUG_HOLD_UNTIL_FULL_MS");
}

static int export_profile_configure_from_env(void)
{
    const char * enabled = getenv("MLVAPP_EXPORT_STAGE_PROFILER");
    const char * output_path = getenv("MLVAPP_EXPORT_STAGE_PROFILE_FILE");
    const char * build_id = getenv("MLVAPP_EXPORT_STAGE_PROFILE_BUILD_ID");

    if(!export_profile_env_truthy(enabled) || output_path == NULL || output_path[0] == '\0')
    {
        return 0;
    }

    if(strcmp(g_export_profile_path, output_path) != 0)
    {
        if(g_export_profile_path[0] != '\0')
        {
            export_profile_write_json();
        }
        export_profile_reset_frames();
    }

    export_profile_copy_string(g_export_profile_path, sizeof(g_export_profile_path), output_path);
    if(build_id != NULL && build_id[0] != '\0')
    {
        export_profile_copy_string(g_export_profile_build_id, sizeof(g_export_profile_build_id), build_id);
    }
    else
    {
        export_profile_copy_string(g_export_profile_build_id,
                                   sizeof(g_export_profile_build_id),
                                   __DATE__ " " __TIME__);
    }

    if(!g_export_profile_atexit_registered)
    {
        atexit(export_profile_write_json);
        g_export_profile_atexit_registered = 1;
    }

    return 1;
}

static void export_profile_frame_begin(exportProfileFrame_t * frame,
                                       mlvObject_t * mlv_data,
                                       dngObject_t * dng_data,
                                       uint32_t frame_index)
{
    if(frame == NULL) return;
    memset(frame, 0, sizeof(*frame));
    pthread_mutex_lock(&g_export_profile_mutex);
    if(!export_profile_configure_from_env())
    {
        pthread_mutex_unlock(&g_export_profile_mutex);
        return;
    }

    const double frame_start_ms = export_profile_now_ms();
    frame->active = 1;
    frame->frame_index = frame_index;
    frame->raw_input_state = dng_data ? dng_data->raw_input_state : -1;
    frame->raw_output_state = dng_data ? dng_data->raw_output_state : -1;
    frame->success = 0;
    if(g_export_profile_last_frame_finish_ms > 0.0)
    {
        const double idle_ms = frame_start_ms - g_export_profile_last_frame_finish_ms;
        frame->stages_ms[EXPORT_PROFILE_QUEUE_IDLE] = idle_ms > 0.0 ? idle_ms : 0.0;
        frame->stage_mask |= (1u << EXPORT_PROFILE_QUEUE_IDLE);
    }
    if(g_export_profile_last_producer_finish_ms > 0.0)
    {
        const double idle_ms = frame_start_ms - g_export_profile_last_producer_finish_ms;
        frame->stages_ms[EXPORT_PROFILE_PRODUCER_QUEUE_IDLE] = idle_ms > 0.0 ? idle_ms : 0.0;
        frame->stage_mask |= (1u << EXPORT_PROFILE_PRODUCER_QUEUE_IDLE);
    }
    export_profile_copy_string(frame->clip_path,
                               sizeof(frame->clip_path),
                               mlv_data ? mlv_data->path : "");
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static double export_profile_stage_begin(exportProfileFrame_t * frame)
{
    return (frame != NULL && frame->active) ? export_profile_now_ms() : 0.0;
}

static void export_profile_stage_end(exportProfileFrame_t * frame,
                                     exportProfileStage_t stage,
                                     double start_ms)
{
    if(frame == NULL || !frame->active || stage < 0 || stage >= EXPORT_PROFILE_STAGE_COUNT)
    {
        return;
    }
    frame->stages_ms[stage] += export_profile_now_ms() - start_ms;
    frame->stage_mask |= (1u << stage);
}

static void export_profile_stage_set(exportProfileFrame_t * frame,
                                     exportProfileStage_t stage,
                                     double value_ms)
{
    if(frame == NULL || !frame->active || stage < 0 || stage >= EXPORT_PROFILE_STAGE_COUNT)
    {
        return;
    }
    frame->stages_ms[stage] = value_ms > 0.0 ? value_ms : 0.0;
    frame->stage_mask |= (1u << stage);
}

static void export_profile_note_llrawproc_breakdown(exportProfileFrame_t * frame)
{
    const double dark_frame_ms = llrpGetLastDarkFrameMilliseconds();
    const double vertical_stripes_ms = llrpGetLastVerticalStripesMilliseconds();
    const double focus_pixels_ms = llrpGetLastFocusPixelsMilliseconds();
    const double bad_pixels_ms = llrpGetLastBadPixelsMilliseconds();
    const double pattern_noise_ms = llrpGetLastPatternNoiseMilliseconds();
    const double dual_iso_ms = llrpGetLastDualIsoMilliseconds();
    const double chroma_smooth_ms = llrpGetLastChromaSmoothMilliseconds();
    const double total_ms = llrpGetLastTotalMilliseconds();
    const double known_ms =
        dark_frame_ms +
        vertical_stripes_ms +
        focus_pixels_ms +
        bad_pixels_ms +
        pattern_noise_ms +
        dual_iso_ms +
        chroma_smooth_ms;

    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_TOTAL, total_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_DARK_FRAME, dark_frame_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_VERTICAL_STRIPES, vertical_stripes_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_FOCUS_PIXELS, focus_pixels_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_BAD_PIXELS, bad_pixels_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_PATTERN_NOISE, pattern_noise_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_PRE_DUALISO_FIX, llrpGetLastPreDualIsoFixMilliseconds());
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_DUAL_ISO, dual_iso_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_CHROMA_SMOOTH, chroma_smooth_ms);
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_SHARED_LOCK, llrpGetLastSharedLockMilliseconds());
    export_profile_stage_set(frame,
                             EXPORT_PROFILE_LLRAWPROC_DUALISO_REFINE_LOCK,
                             llrpGetLastDualIsoRefineLockMilliseconds());
    export_profile_stage_set(frame, EXPORT_PROFILE_LLRAWPROC_PUBLISH_LOCK, llrpGetLastPublishLockMilliseconds());
    export_profile_stage_set(frame,
                             EXPORT_PROFILE_LLRAWPROC_OTHER,
                             total_ms > known_ms ? total_ms - known_ms : 0.0);
}

static void export_profile_note_gpu_export_telemetry(exportProfileFrame_t * frame)
{
    llrpGpuExportTelemetry_t telemetry = { 0 };
    if(frame == NULL || !frame->active)
    {
        return;
    }

    llrpGetLastGpuExportTelemetry(&telemetry);
    frame->gpu_export_attempted = telemetry.attempted ? 1 : 0;
    frame->gpu_export_rc = telemetry.rc;
    frame->gpu_export_replaced = telemetry.replaced ? 1 : 0;
    frame->gpu_export_trusted = telemetry.trusted ? 1 : 0;
    frame->gpu_export_allocated_bytes_valid =
        telemetry.allocated_bytes_valid ? 1 : 0;
    frame->gpu_export_allocated_bytes = telemetry.allocated_bytes;
    frame->gpu_export_skip_code = telemetry.skip_code;
}

static void export_profile_note_dng_compress_bytes(exportProfileFrame_t * frame,
                                                   uint64_t input_bytes,
                                                   uint64_t output_bytes,
                                                   int valid)
{
    if(frame == NULL || !frame->active)
    {
        return;
    }

    frame->dng_compress_bytes_valid = valid ? 1 : 0;
    frame->dng_compress_input_bytes = input_bytes;
    frame->dng_compress_output_bytes = output_bytes;
}

static uint64_t dng_compress_input_bytes_for_frame(const mlvObject_t * mlv_data)
{
    if(mlv_data == NULL)
    {
        return 0;
    }

    return (uint64_t)mlv_data->RAWI.xRes *
           (uint64_t)mlv_data->RAWI.yRes *
           (uint64_t)sizeof(uint16_t);
}

static void export_profile_note_producer_finish(exportProfileFrame_t * frame)
{
    if(frame == NULL || !frame->active) return;
    const double finish_ms = export_profile_now_ms();
    pthread_mutex_lock(&g_export_profile_mutex);
    g_export_profile_last_producer_finish_ms = finish_ms;
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static void export_profile_producer_finish(exportProfileFrame_t * frame,
                                           double profile_frame_start)
{
    if(frame == NULL || !frame->active) return;
    export_profile_stage_end(frame, EXPORT_PROFILE_PRODUCER_FRAME, profile_frame_start);
    export_profile_note_producer_finish(frame);
}

static void export_profile_frame_finish(exportProfileFrame_t * frame, int success)
{
    exportProfileFrame_t * next = NULL;
    const double finish_ms = export_profile_now_ms();
    if(frame == NULL || !frame->active) return;

    pthread_mutex_lock(&g_export_profile_mutex);
    frame->success = success ? 1 : 0;
    if(g_export_profile_frame_count == g_export_profile_frame_capacity)
    {
        size_t new_capacity = g_export_profile_frame_capacity ? g_export_profile_frame_capacity * 2 : 64;
        exportProfileFrame_t * grown = (exportProfileFrame_t*)realloc(g_export_profile_frames,
            new_capacity * sizeof(exportProfileFrame_t));
        if(grown == NULL)
        {
            g_export_profile_last_frame_finish_ms = finish_ms;
            pthread_mutex_unlock(&g_export_profile_mutex);
            return;
        }
        g_export_profile_frames = grown;
        g_export_profile_frame_capacity = new_capacity;
    }

    next = &g_export_profile_frames[g_export_profile_frame_count++];
    *next = *frame;
    g_export_profile_last_frame_finish_ms = finish_ms;
    pthread_mutex_unlock(&g_export_profile_mutex);
}

static int export_profile_double_compare(const void * lhs, const void * rhs)
{
    const double a = *(const double*)lhs;
    const double b = *(const double*)rhs;
    return (a > b) - (a < b);
}

static int export_profile_stage_value(const exportProfileFrame_t * frame,
                                      const char * stage_name,
                                      double * value)
{
    if(frame == NULL || value == NULL) return 0;

    if(strcmp(stage_name, "raw_read_decode_unpack_ms") == 0)
    {
        const uint32_t raw_mask =
            (1u << EXPORT_PROFILE_RAW_READ) |
            (1u << EXPORT_PROFILE_RAW_DECODE) |
            (1u << EXPORT_PROFILE_RAW_UNPACK);
        if((frame->stage_mask & raw_mask) == 0) return 0;
        *value = frame->stages_ms[EXPORT_PROFILE_RAW_READ] +
                 frame->stages_ms[EXPORT_PROFILE_RAW_DECODE] +
                 frame->stages_ms[EXPORT_PROFILE_RAW_UNPACK];
        return 1;
    }

    if(strcmp(stage_name, "writer_completion_lag_ms") == 0)
    {
        double frame_total_ms = 0.0;
        double producer_frame_ms = 0.0;
        if(!export_profile_stage_value(frame, "frame_total_ms", &frame_total_ms) ||
           !export_profile_stage_value(frame, "producer_frame_ms", &producer_frame_ms))
        {
            return 0;
        }
        *value = frame_total_ms >= producer_frame_ms
            ? frame_total_ms - producer_frame_ms
            : 0.0;
        return 1;
    }

    for(int stage = 0; stage < EXPORT_PROFILE_STAGE_COUNT; stage++)
    {
        if(strcmp(stage_name, export_profile_stage_name((exportProfileStage_t)stage)) == 0)
        {
            if((frame->stage_mask & (1u << stage)) == 0) return 0;
            *value = frame->stages_ms[stage];
            return 1;
        }
    }

    return 0;
}

static void export_profile_write_json_string(FILE * file, const char * value)
{
    fputc('"', file);
    if(value)
    {
        for(const unsigned char * p = (const unsigned char*)value; *p; p++)
        {
            switch(*p)
            {
                case '\\': fputs("\\\\", file); break;
                case '"':  fputs("\\\"", file); break;
                case '\b': fputs("\\b", file); break;
                case '\f': fputs("\\f", file); break;
                case '\n': fputs("\\n", file); break;
                case '\r': fputs("\\r", file); break;
                case '\t': fputs("\\t", file); break;
                default:
                    if(*p < 0x20) fprintf(file, "\\u%04x", *p);
                    else fputc(*p, file);
                    break;
            }
        }
    }
    fputc('"', file);
}

static const char * export_profile_env_nonempty(const char * name)
{
    const char * value = getenv(name);
    return (value != NULL && value[0] != '\0') ? value : NULL;
}

static void export_profile_write_json_string_or_null(FILE * file,
                                                     const char * value)
{
    if(value != NULL && value[0] != '\0')
    {
        export_profile_write_json_string(file, value);
    }
    else
    {
        fputs("null", file);
    }
}

static void export_profile_write_json_integer_or_null(FILE * file,
                                                      const char * value)
{
    char * end = NULL;
    if(value == NULL || value[0] == '\0')
    {
        fputs("null", file);
        return;
    }

    long long parsed = strtoll(value, &end, 10);
    if(end == value || (end != NULL && *end != '\0') || parsed < 0)
    {
        fputs("null", file);
        return;
    }

    fprintf(file, "%lld", parsed);
}

static void export_profile_write_machine_fingerprint(FILE * file)
{
    const char * build_sha =
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_BUILD_SHA");
    if(build_sha == NULL || build_sha[0] == '\0')
    {
        build_sha = (g_export_profile_build_id[0] != '\0')
            ? g_export_profile_build_id
            : MLVAPP_GIT_SHA;
    }

    fputs("{\"schema\":\"machine-fingerprint.v1\"", file);
    fputs(",\"build_sha\":", file);
    export_profile_write_json_string_or_null(file, build_sha);
    fputs(",\"gpu_name\":", file);
    export_profile_write_json_string_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_GPU_NAME"));
    fputs(",\"gpu_driver_version\":", file);
    export_profile_write_json_string_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_GPU_DRIVER_VERSION"));
    fputs(",\"gpu_compute_capability\":", file);
    export_profile_write_json_string_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_GPU_COMPUTE_CAPABILITY"));
    fputs(",\"gpu_vram_total_mb\":", file);
    export_profile_write_json_integer_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_GPU_VRAM_TOTAL_MB"));
    fputs(",\"cpu_model\":", file);
    export_profile_write_json_string_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_CPU_MODEL"));
    fputs(",\"cpu_cores\":", file);
    export_profile_write_json_integer_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_CPU_CORES"));
    fputs(",\"cpu_threads\":", file);
    export_profile_write_json_integer_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_CPU_THREADS"));
    fputs(",\"ram_total_mb\":", file);
    export_profile_write_json_integer_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_RAM_TOTAL_MB"));
    fputs(",\"os_version\":", file);
    export_profile_write_json_string_or_null(
        file,
        export_profile_env_nonempty("MLVAPP_MACHINE_FINGERPRINT_OS_VERSION"));
    fputs("}", file);
}

static void export_profile_write_stage_stats(FILE * file,
                                             const char * name,
                                             int comma)
{
    double * values = NULL;
    size_t count = 0;
    double sum = 0.0;

    values = (double*)malloc(g_export_profile_frame_count * sizeof(double));
    if(values == NULL)
    {
        if(comma) fputs(",\n", file);
        fprintf(file, "    ");
        export_profile_write_json_string(file, name);
        fputs(": {\"samples\":0,\"avg_ms\":0,\"p50_ms\":0,\"p95_ms\":0}", file);
        return;
    }

    for(size_t i = 0; i < g_export_profile_frame_count; i++)
    {
        double value = 0.0;
        if(export_profile_stage_value(&g_export_profile_frames[i], name, &value))
        {
            values[count++] = value;
            sum += value;
        }
    }

    qsort(values, count, sizeof(double), export_profile_double_compare);

    const double avg = count ? sum / (double)count : 0.0;
    const double p50 = count ? values[(size_t)((count - 1) * 0.50)] : 0.0;
    const double p95 = count ? values[(size_t)((count - 1) * 0.95)] : 0.0;

    if(comma) fputs(",\n", file);
    fprintf(file, "    ");
    export_profile_write_json_string(file, name);
    fprintf(file,
            ": {\"samples\":%u,\"avg_ms\":%.6f,\"p50_ms\":%.6f,\"p95_ms\":%.6f}",
            (unsigned)count,
            avg,
            p50,
            p95);
    free(values);
}

typedef struct
{
    const char * limiting_stage;
    const char * source_stage;
    const char * suggested_optimization;
    double median_ms;
    double p95_ms;
    unsigned samples;
} exportProfileBottleneck_t;

static int export_profile_compute_stage_percentiles(const char * name,
                                                    double * median_ms,
                                                    double * p95_ms,
                                                    unsigned * samples)
{
    double * values = NULL;
    size_t count = 0;

    if(median_ms) *median_ms = 0.0;
    if(p95_ms) *p95_ms = 0.0;
    if(samples) *samples = 0;

    values = (double*)malloc(g_export_profile_frame_count * sizeof(double));
    if(values == NULL) return 0;

    for(size_t i = 0; i < g_export_profile_frame_count; i++)
    {
        double value = 0.0;
        if(export_profile_stage_value(&g_export_profile_frames[i], name, &value))
        {
            values[count++] = value;
        }
    }

    if(count == 0)
    {
        free(values);
        return 0;
    }

    qsort(values, count, sizeof(double), export_profile_double_compare);
    if(median_ms) *median_ms = values[(size_t)((count - 1) * 0.50)];
    if(p95_ms) *p95_ms = values[(size_t)((count - 1) * 0.95)];
    if(samples) *samples = (unsigned)count;
    free(values);
    return 1;
}

static const char * export_profile_suggested_optimization(const char * bottleneck)
{
    if(strcmp(bottleneck, "decode-bound") == 0)
        return "parallelize_lj92_decode";
    if(strcmp(bottleneck, "recon-bound") == 0)
        return "optimize_dual_iso_recon";
    if(strcmp(bottleneck, "compress-bound") == 0)
        return "parallelize_or_offload_dng_compress";
    if(strcmp(bottleneck, "write-bound") == 0)
        return "increase_async_writer_depth_or_storage_bandwidth";
    return "collect_more_stage_samples";
}

static exportProfileBottleneck_t export_profile_select_bottleneck(void)
{
    static const struct
    {
        const char * label;
        const char * stage_name;
    } candidates[] =
    {
        { "decode-bound", "raw_read_decode_unpack_ms" },
        { "recon-bound", "llrawproc_total_ms" },
        { "compress-bound", "dng_compress_ms" },
        { "write-bound", "disk_write_ms" }
    };

    exportProfileBottleneck_t result;
    memset(&result, 0, sizeof(result));
    result.limiting_stage = "unknown";
    result.source_stage = "unknown";
    result.suggested_optimization =
        export_profile_suggested_optimization(result.limiting_stage);

    for(size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++)
    {
        double median_ms = 0.0;
        double p95_ms = 0.0;
        unsigned samples = 0;
        if(!export_profile_compute_stage_percentiles(candidates[i].stage_name,
                                                     &median_ms,
                                                     &p95_ms,
                                                     &samples))
        {
            continue;
        }
        if(strcmp(result.limiting_stage, "unknown") == 0
        || median_ms > result.median_ms)
        {
            result.limiting_stage = candidates[i].label;
            result.source_stage = candidates[i].stage_name;
            result.suggested_optimization =
                export_profile_suggested_optimization(candidates[i].label);
            result.median_ms = median_ms;
            result.p95_ms = p95_ms;
            result.samples = samples;
        }
    }

    return result;
}

static void export_profile_write_bottleneck(FILE * file,
                                            const exportProfileBottleneck_t * bottleneck)
{
    const exportProfileBottleneck_t fallback = export_profile_select_bottleneck();
    const exportProfileBottleneck_t * value =
        bottleneck != NULL ? bottleneck : &fallback;

    fputs("{\"schema\":\"mlvapp.bottleneck-rollup.v1\"", file);
    fputs(",\"limiting_stage\":", file);
    export_profile_write_json_string(file, value->limiting_stage);
    fputs(",\"source_stage\":", file);
    export_profile_write_json_string(file, value->source_stage);
    fprintf(file,
            ",\"median_ms\":%.6f,\"p95_ms\":%.6f,\"samples\":%u",
            value->median_ms,
            value->p95_ms,
            value->samples);
    fputs(",\"suggested_optimization\":", file);
    export_profile_write_json_string(file, value->suggested_optimization);
    fputs("}", file);
}

static void export_profile_append_perf_field_log(
    const exportProfileBottleneck_t * bottleneck)
{
    const char * path = export_profile_env_nonempty("MLVAPP_PERF_FIELD_LOG_PATH");
    FILE * log = NULL;
    if(!export_profile_env_truthy(getenv("MLVAPP_PERF_FIELD_LOG")) || path == NULL)
    {
        return;
    }
    if(g_export_profile_field_log_appended)
    {
        return;
    }

    log = fopen(path, "ab");
    if(log == NULL) return;

    g_export_profile_field_log_appended = 1;
    fputs("{\"schema\":\"mlvapp.perf-field-log.v1\"", log);
    fputs(",\"kind\":\"export\"", log);
    fputs(",\"machineFingerprint\":", log);
    export_profile_write_machine_fingerprint(log);
    fprintf(log, ",\"frame_count\":%u", (unsigned)g_export_profile_frame_count);
    fputs(",\"pipeline_counts\":{}", log);
    fputs(",\"presented_fps\":null,\"timeline_fps\":null", log);
    fputs(",\"fallback_count\":", log);
    fprintf(log, "%u", 0u);
    fputs(",\"fallback_reason_histogram\":{}", log);
    fputs(",\"bottleneck\":", log);
    export_profile_write_bottleneck(log, bottleneck);
    fputs(",\"suggested_optimization\":", log);
    export_profile_write_json_string(
        log,
        bottleneck != NULL
            ? bottleneck->suggested_optimization
            : export_profile_suggested_optimization("unknown"));
    fputs("}\n", log);
    fclose(log);
}

static void export_profile_write_json(void)
{
    FILE * file = NULL;
    time_t now;
    struct tm * utc;
    char generated[64] = "";
    unsigned gpu_export_attempted_frames = 0;
    unsigned gpu_export_replaced_frames = 0;
    unsigned gpu_export_trusted_frames = 0;
    unsigned gpu_export_allocated_bytes_valid_frames = 0;
    unsigned gpu_export_skipped_frames = 0;
    unsigned gpu_export_skip_counts[LLRP_GPU_EXPORT_SKIP_COUNT] = {0};
    uint64_t gpu_export_max_allocated_bytes = 0;
    unsigned dng_compress_bytes_valid_frames = 0;
    uint64_t dng_compress_input_bytes_total = 0;
    uint64_t dng_compress_output_bytes_total = 0;
    int async_writer_compress_used = 0;
    exportProfileBottleneck_t bottleneck;

    if(g_export_profile_write_in_progress) return;
    if(g_export_profile_path[0] == '\0' || g_export_profile_frame_count == 0) return;

    for(size_t i = 0; i < g_export_profile_frame_count; i++)
    {
        const exportProfileFrame_t * frame = &g_export_profile_frames[i];
        if(frame->gpu_export_attempted)
        {
            gpu_export_attempted_frames++;
        }
        if(frame->gpu_export_replaced)
        {
            gpu_export_replaced_frames++;
        }
        if(frame->gpu_export_trusted)
        {
            gpu_export_trusted_frames++;
        }
        if(frame->gpu_export_skip_code > LLRP_GPU_EXPORT_SKIP_NONE
        && frame->gpu_export_skip_code < LLRP_GPU_EXPORT_SKIP_COUNT)
        {
            gpu_export_skipped_frames++;
            gpu_export_skip_counts[frame->gpu_export_skip_code]++;
        }
        if(frame->gpu_export_allocated_bytes_valid)
        {
            gpu_export_allocated_bytes_valid_frames++;
            if(frame->gpu_export_allocated_bytes > gpu_export_max_allocated_bytes)
            {
                gpu_export_max_allocated_bytes = frame->gpu_export_allocated_bytes;
            }
        }
        if(frame->dng_compress_bytes_valid)
        {
            dng_compress_bytes_valid_frames++;
            dng_compress_input_bytes_total += frame->dng_compress_input_bytes;
            dng_compress_output_bytes_total += frame->dng_compress_output_bytes;
        }
    }
    /* This writer is also called from profile-path rollover while
     * g_export_profile_mutex is already held. The rest of this JSON snapshot
     * reads profiler globals lock-free, so do not re-enter the mutex here. */
    async_writer_compress_used = g_export_profile_async_writer_compress_used;
    bottleneck = export_profile_select_bottleneck();

    g_export_profile_write_in_progress = 1;
    file = fopen(g_export_profile_path, "wb");
    if(file == NULL)
    {
        g_export_profile_write_in_progress = 0;
        return;
    }

    now = time(NULL);
    utc = gmtime(&now);
    if(utc)
    {
        strftime(generated, sizeof(generated), "%Y-%m-%dT%H:%M:%SZ", utc);
    }

    fputs("{\n", file);
    fputs("  \"schema\":\"mlvapp.export_stage_profile.v1\",\n", file);
    fputs("  \"enabled_by\":\"MLVAPP_EXPORT_STAGE_PROFILER\",\n", file);
    fputs("  \"profile_file_env\":\"MLVAPP_EXPORT_STAGE_PROFILE_FILE\",\n", file);
    fputs("  \"build_id\":", file);
    export_profile_write_json_string(file, g_export_profile_build_id);
    fputs(",\n", file);
    fprintf(file,
            "  \"payload_handoff_env_enabled\":%s,\n",
            export_profile_env_truthy(getenv("MLVAPP_CDNG_EXPORT_PAYLOAD_HANDOFF"))
                ? "true"
                : "false");
    fprintf(file,
            "  \"async_writer_env_enabled\":%s,\n",
            export_profile_env_truthy(getenv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER"))
                ? "true"
                : "false");
    fprintf(file,
            "  \"async_writer_compress_env_enabled\":%s,\n",
            dng_async_writer_compress_env_enabled() ? "true" : "false");
    fputs("  \"generated_utc\":", file);
    export_profile_write_json_string(file, generated);
    fputs(",\n", file);
    fputs("  \"machineFingerprint\":", file);
    export_profile_write_machine_fingerprint(file);
    fputs(",\n", file);
    fprintf(file, "  \"frame_count\":%u,\n", (unsigned)g_export_profile_frame_count);
    fputs("  \"queue_idle_supported\":true,\n", file);
    fprintf(file,
            "  \"async_writer_thread_count\":%u,\n",
            g_export_profile_async_writer_thread_count);
    fprintf(file,
            "  \"async_writer_queue_capacity\":%u,\n",
            g_export_profile_async_writer_queue_capacity);
    fprintf(file,
            "  \"async_writer_max_queued\":%u,\n",
            g_export_profile_async_writer_max_queued);
    fprintf(file,
            "  \"async_writer_jobs_started\":%u,\n",
            g_export_profile_async_writer_jobs_started);
    fprintf(file,
            "  \"async_writer_jobs_finished\":%u,\n",
            g_export_profile_async_writer_jobs_finished);
    fprintf(file,
            "  \"async_writer_max_active\":%u,\n",
            g_export_profile_async_writer_max_active);
    fprintf(file,
            "  \"async_writer_debug_delay_ms\":%u,\n",
            dng_payload_writer_debug_delay_ms_from_env());
    fprintf(file,
            "  \"gpu_export_attempted_frames\":%u,\n",
            gpu_export_attempted_frames);
    fprintf(file,
            "  \"gpu_export_replaced_frames\":%u,\n",
            gpu_export_replaced_frames);
    fprintf(file,
            "  \"gpu_export_trusted_frames\":%u,\n",
            gpu_export_trusted_frames);
    fprintf(file,
            "  \"gpu_export_allocated_bytes_valid_frames\":%u,\n",
            gpu_export_allocated_bytes_valid_frames);
    fprintf(file,
            "  \"gpu_export_max_allocated_bytes\":%llu,\n",
            (unsigned long long)gpu_export_max_allocated_bytes);
    fprintf(file,
            "  \"gpu_export_skipped_frames\":%u,\n",
            gpu_export_skipped_frames);
    fputs("  \"gpu_export_skip_reason_counts\":{", file);
    for(int skip_code = LLRP_GPU_EXPORT_SKIP_DISABLED;
        skip_code < LLRP_GPU_EXPORT_SKIP_COUNT;
        skip_code++)
    {
        if(skip_code > LLRP_GPU_EXPORT_SKIP_DISABLED)
        {
            fputs(",", file);
        }
        export_profile_write_json_string(file,
            export_profile_gpu_export_skip_reason(skip_code));
        fprintf(file, ":%u", gpu_export_skip_counts[skip_code]);
    }
    fputs("},\n", file);
    fprintf(file,
            "  \"dng_compress_bytes_valid_frames\":%u,\n",
            dng_compress_bytes_valid_frames);
    fprintf(file,
            "  \"dng_compress_input_bytes_total\":%llu,\n",
            (unsigned long long)dng_compress_input_bytes_total);
    fprintf(file,
            "  \"dng_compress_output_bytes_total\":%llu,\n",
            (unsigned long long)dng_compress_output_bytes_total);
    fputs("  \"dng_compress_placement\":", file);
    export_profile_write_json_string(
        file,
        async_writer_compress_used
            ? DNG_COMPRESS_PLACEMENT_WRITER
            : DNG_COMPRESS_PLACEMENT_PRODUCER);
    fputs(",\n", file);
    fprintf(file,
            "  \"async_writer_can_overlap_dng_compress\":%s,\n",
            async_writer_compress_used ? "true" : "false");
    fputs("  \"stages\":{\n", file);
    export_profile_write_stage_stats(file, "raw_read_decode_unpack_ms", 0);
    export_profile_write_stage_stats(file, "raw_read_ms", 1);
    export_profile_write_stage_stats(file, "raw_decode_ms", 1);
    export_profile_write_stage_stats(file, "raw_unpack_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_total_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_dark_frame_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_vertical_stripes_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_focus_pixels_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_bad_pixels_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_pattern_noise_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_pre_dualiso_fix_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_dual_iso_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_chroma_smooth_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_shared_lock_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_dualiso_refine_lock_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_publish_lock_ms", 1);
    export_profile_write_stage_stats(file, "llrawproc_other_ms", 1);
    export_profile_write_stage_stats(file, "dng_header_ms", 1);
    export_profile_write_stage_stats(file, "dng_pack_ms", 1);
    export_profile_write_stage_stats(file, "dng_compress_ms", 1);
    export_profile_write_stage_stats(file, "dng_compress_encode_ms", 1);
    export_profile_write_stage_stats(file, "dng_compress_copy_ms", 1);
    export_profile_write_stage_stats(file, "dng_compress_cleanup_ms", 1);
    export_profile_write_stage_stats(file, "payload_clone_ms", 1);
    export_profile_write_stage_stats(file, "disk_write_ms", 1);
    export_profile_write_stage_stats(file, "writer_queue_wait_ms", 1);
    export_profile_write_stage_stats(file, "queue_idle_ms", 1);
    export_profile_write_stage_stats(file, "producer_queue_idle_ms", 1);
    export_profile_write_stage_stats(file, "producer_frame_ms", 1);
    export_profile_write_stage_stats(file, "writer_completion_lag_ms", 1);
    export_profile_write_stage_stats(file, "frame_total_ms", 1);
    fputs("  },\n", file);
    fputs("  \"bottleneck\":", file);
    export_profile_write_bottleneck(file, &bottleneck);
    fputs(",\n", file);
    fputs("  \"frames\":[\n", file);
    for(size_t i = 0; i < g_export_profile_frame_count; i++)
    {
        const exportProfileFrame_t * frame = &g_export_profile_frames[i];
        if(i) fputs(",\n", file);
        fputs("    {", file);
        fputs("\"clip\":", file);
        export_profile_write_json_string(file, frame->clip_path);
        fprintf(file,
                ",\"frame_index\":%u,\"raw_input_state\":",
                frame->frame_index);
        export_profile_write_json_string(file, export_profile_raw_state_name(frame->raw_input_state));
        fputs(",\"raw_output_state\":", file);
        export_profile_write_json_string(file, export_profile_raw_state_name(frame->raw_output_state));
        fprintf(file, ",\"success\":%s", frame->success ? "true" : "false");
        fprintf(file,
                ",\"gpu_export_attempted\":%s,\"gpu_export_rc\":%d"
                ",\"gpu_export_replaced\":%s"
                ",\"gpu_export_trusted\":%s"
                ",\"gpu_export_allocated_bytes_valid\":%s"
                ",\"gpu_export_allocated_bytes\":%llu"
                ",\"gpu_export_skip_code\":%d"
                ",\"gpu_export_skip_reason\":",
                frame->gpu_export_attempted ? "true" : "false",
                frame->gpu_export_rc,
                frame->gpu_export_replaced ? "true" : "false",
                frame->gpu_export_trusted ? "true" : "false",
                frame->gpu_export_allocated_bytes_valid ? "true" : "false",
                (unsigned long long)frame->gpu_export_allocated_bytes,
                frame->gpu_export_skip_code);
        export_profile_write_json_string(file,
            export_profile_gpu_export_skip_reason(frame->gpu_export_skip_code));
        fprintf(file,
                ",\"dng_compress_bytes_valid\":%s"
                ",\"dng_compress_input_bytes\":%llu"
                ",\"dng_compress_output_bytes\":%llu",
                frame->dng_compress_bytes_valid ? "true" : "false",
                (unsigned long long)frame->dng_compress_input_bytes,
                (unsigned long long)frame->dng_compress_output_bytes);

        for(int stage = 0; stage < EXPORT_PROFILE_STAGE_COUNT; stage++)
        {
            if((frame->stage_mask & (1u << stage)) == 0) continue;
            fprintf(file, ",");
            export_profile_write_json_string(file, export_profile_stage_name((exportProfileStage_t)stage));
            fprintf(file, ":%.6f", frame->stages_ms[stage]);
        }

        {
            double raw_total = 0.0;
            if(export_profile_stage_value(frame, "raw_read_decode_unpack_ms", &raw_total))
            {
                fputs(",", file);
                export_profile_write_json_string(file, "raw_read_decode_unpack_ms");
                fprintf(file, ":%.6f", raw_total);
            }
        }
        {
            double writer_lag = 0.0;
            if(export_profile_stage_value(frame, "writer_completion_lag_ms", &writer_lag))
            {
                fputs(",", file);
                export_profile_write_json_string(file, "writer_completion_lag_ms");
                fprintf(file, ":%.6f", writer_lag);
            }
        }

        fputs("}", file);
    }
    fputs("\n  ]\n", file);
    fputs("}\n", file);
    fclose(file);
    export_profile_append_perf_field_log(&bottleneck);
    g_export_profile_write_in_progress = 0;
}

//MLV WB modes
enum
{
    WB_AUTO         = 0,
    WB_SUNNY        = 1,
    WB_SHADE        = 8,
    WB_CLOUDY       = 2,
    WB_TUNGSTEN     = 3,
    WB_FLUORESCENT  = 4,
    WB_FLASH        = 5,
    WB_CUSTOM       = 6,
    WB_KELVIN       = 9
};

/*****************************************************************************************************
 * Kelvin/Green to RGB Multipliers from UFRAW
 *****************************************************************************************************/

#define COLORS 3

/* Convert between Temperature and RGB.
 * Base on information from http://www.brucelindbloom.com/
 * The fit for D-illuminant between 4000K and 23000K are from CIE
 * The generalization to 2000K < T < 4000K and the blackbody fits
 * are my own and should be taken with a grain of salt.
 */
static const double XYZ_to_RGB[3][3] = {
    { 3.24071,    -0.969258,  0.0556352 },
    { -1.53726,    1.87599,    -0.203996 },
    { -0.498571,    0.0415557,  1.05707 }
};

static const double xyz_rgb[3][3] = {
    { 0.412453, 0.357580, 0.180423 },
    { 0.212671, 0.715160, 0.072169 },
    { 0.019334, 0.119193, 0.950227 }
};

static inline void temperature_to_RGB(double T, double RGB[3])
{
    int c;
    double xD, yD, X, Y, Z, max;
    // Fit for CIE Daylight illuminant
    if (T <= 4000)
    {
        xD = 0.27475e9 / (T * T * T) - 0.98598e6 / (T * T) + 1.17444e3 / T + 0.145986;
    }
    else if (T <= 7000)
    {
        xD = -4.6070e9 / (T * T * T) + 2.9678e6 / (T * T) + 0.09911e3 / T + 0.244063;
    }
    else
    {
        xD = -2.0064e9 / (T * T * T) + 1.9018e6 / (T * T) + 0.24748e3 / T + 0.237040;
    }
    yD = -3 * xD * xD + 2.87 * xD - 0.275;
    
    // Fit for Blackbody using CIE standard observer function at 2 degrees
    //xD = -1.8596e9/(T*T*T) + 1.37686e6/(T*T) + 0.360496e3/T + 0.232632;
    //yD = -2.6046*xD*xD + 2.6106*xD - 0.239156;
    
    // Fit for Blackbody using CIE standard observer function at 10 degrees
    //xD = -1.98883e9/(T*T*T) + 1.45155e6/(T*T) + 0.364774e3/T + 0.231136;
    //yD = -2.35563*xD*xD + 2.39688*xD - 0.196035;
    
    X = xD / yD;
    Y = 1;
    Z = (1 - xD - yD) / yD;
    max = 0;
    for (c = 0; c < 3; c++) {
        RGB[c] = X * XYZ_to_RGB[0][c] + Y * XYZ_to_RGB[1][c] + Z * XYZ_to_RGB[2][c];
        if (RGB[c] > max) max = RGB[c];
    }
    for (c = 0; c < 3; c++) RGB[c] = RGB[c] / max;
}

static inline void pseudoinverse (double (*in)[3], double (*out)[3], int size)
{
    double work[3][6], num;
    int i, j, k;
    
    for (i=0; i < 3; i++) {
        for (j=0; j < 6; j++)
            work[i][j] = j == i+3;
        for (j=0; j < 3; j++)
            for (k=0; k < size; k++)
                work[i][j] += in[k][i] * in[k][j];
    }
    for (i=0; i < 3; i++) {
        num = work[i][i];
        for (j=0; j < 6; j++)
            work[i][j] /= num;
        for (k=0; k < 3; k++) {
            if (k==i) continue;
            num = work[k][i];
            for (j=0; j < 6; j++)
                work[k][j] -= work[i][j] * num;
        }
    }
    for (i=0; i < size; i++)
        for (j=0; j < 3; j++)
            for (out[i][j]=k=0; k < 3; k++)
                out[i][j] += work[j][k+3] * in[i][k];
}

static inline void cam_xyz_coeff (double cam_xyz[4][3], float pre_mul[4], float rgb_cam[3][4])
{
    double cam_rgb[4][3], inverse[4][3], num;
    int i, j, k;
    
    for (i=0; i < COLORS; i++)                /* Multiply out XYZ colorspace */
        for (j=0; j < 3; j++)
            for (cam_rgb[i][j] = k=0; k < 3; k++)
                cam_rgb[i][j] += cam_xyz[i][k] * xyz_rgb[k][j];
    
    for (i=0; i < COLORS; i++) {                /* Normalize cam_rgb so that */
        for (num=j=0; j < 3; j++)                /* cam_rgb * (1,1,1) is (1,1,1,1) */
            num += cam_rgb[i][j];
        for (j=0; j < 3; j++)
            cam_rgb[i][j] /= num;
        pre_mul[i] = 1 / num;
    }
    pseudoinverse (cam_rgb, inverse, COLORS);
    for (i=0; i < 3; i++)
        for (j=0; j < COLORS; j++)
            rgb_cam[i][j] = inverse[j][i];
}


static void kelvin_green_to_multipliers(double temperature, double green, double chanMulArray[3], uint32_t cam_id)
{
    float pre_mul[4], rgb_cam[3][4];
    double cam_xyz[4][3];
    double rgbWB[3];
    double cam_rgb[3][3];
    double rgb_cam_transpose[4][3];
    int c, cc, i, j;

    int32_t * color_matrix = camidGetColorMatrix2(cam_id);
    for (i = 0; i < 9; i++)
    {
        cam_xyz[i/3][i%3] = (double)color_matrix[i*2] / (double)color_matrix[i*2 + 1];
    }
    
    for (i = 9; i < 12; i++)
    {
        cam_xyz[i/3][i%3] = 0;
    }
    
    cam_xyz_coeff (cam_xyz, pre_mul, rgb_cam);
    
    for (i = 0; i < 4; i++) for (j = 0; j < 3; j++)
    {
        rgb_cam_transpose[i][j] = rgb_cam[j][i];
    }
    
    pseudoinverse(rgb_cam_transpose, cam_rgb, 3);
    
    temperature_to_RGB(temperature, rgbWB);
    rgbWB[1] = rgbWB[1] / green;
    
    for (c = 0; c < 3; c++)
    {
        double chanMulInv = 0;
        for (cc = 0; cc < 3; cc++)
            chanMulInv += 1 / pre_mul[c] * cam_rgb[c][cc] * rgbWB[cc];
        chanMulArray[c] = 1 / chanMulInv;
    }
    
    /* normalize green multiplier */
    chanMulArray[0] /= chanMulArray[1];
    chanMulArray[2] /= chanMulArray[1];
    chanMulArray[1] = 1;
}

static void get_white_balance(mlv_wbal_hdr_t wbal_hdr, int32_t *wbal, uint32_t cam_id)
{
    if(wbal_hdr.wb_mode == WB_CUSTOM)
    {
        wbal[0] = wbal_hdr.wbgain_r; wbal[1] = wbal_hdr.wbgain_g;
        wbal[2] = wbal_hdr.wbgain_g; wbal[3] = wbal_hdr.wbgain_g;
        wbal[4] = wbal_hdr.wbgain_b; wbal[5] = wbal_hdr.wbgain_g;
    }
    else
    {
        double kelvin = 5500;
        double green = 1.0;
        
        //TODO: G/M shift, not sure how this relates to "green" parameter
        if(wbal_hdr.wb_mode == WB_AUTO || wbal_hdr.wb_mode == WB_KELVIN)
        {
            kelvin = wbal_hdr.kelvin;
        }
        else if(wbal_hdr.wb_mode == WB_SUNNY)
        {
            kelvin = 5500;
        }
        else if(wbal_hdr.wb_mode == WB_SHADE)
        {
            kelvin = 7000;
        }
        else if(wbal_hdr.wb_mode == WB_CLOUDY)
        {
            kelvin = 6000;
        }
        else if(wbal_hdr.wb_mode == WB_TUNGSTEN)
        {
            kelvin = 3200;
        }
        else if(wbal_hdr.wb_mode == WB_FLUORESCENT)
        {
            kelvin = 4000;
        }
        else if(wbal_hdr.wb_mode == WB_FLASH)
        {
            kelvin = 5500;
        }
        double chanMulArray[3];
        kelvin_green_to_multipliers(kelvin, green, chanMulArray, cam_id);
        wbal[0] = 1000000; wbal[1] = (int32_t)(chanMulArray[0] * 1000000);
        wbal[2] = 1000000; wbal[3] = (int32_t)(chanMulArray[1] * 1000000);
        wbal[4] = 1000000; wbal[5] = (int32_t)(chanMulArray[2] * 1000000);
    }
}

/*****************************************************************************************************/

static int32_t dng_clamp_double_to_int32(double value)
{
    if (value > 2147483647.0) return 2147483647;
    if (value < -2147483648.0) return (int32_t)-2147483647 - 1;
    return (int32_t)lrint(value);
}

static void dng_add_srational(int32_t value[2], const int32_t add[2])
{
    const int32_t value_den = value[1] ? value[1] : 1;
    const int32_t add_den = add[1] ? add[1] : 1;
    const double combined =
        ((double)value[0] / (double)value_den) +
        ((double)add[0] / (double)add_den);
    value[0] = dng_clamp_double_to_int32(combined * 10000.0);
    value[1] = 10000;
}


static uint16_t tiff_header[] = { byteOrderII, magicTIFF, 8, 0};

struct directory_entry {
    uint16_t tag;
    uint16_t type;
    uint32_t count;
    uint32_t value;
};

/* CDNG tag codes */
enum
{
    tcTimeCodes             = 51043,
    tcTStop                 = 51058,
    tcReelName              = 51081,
    tcCameraLabel           = 51105,
};

static uint32_t add_array(int32_t * array, uint8_t * buffer, uint32_t * data_offset, size_t length)
{
    uint32_t result = *data_offset;
    memcpy(buffer + result, array, length * sizeof(int32_t));
    *data_offset += length * sizeof(int32_t);
    return result;
}

static uint32_t add_string(const char * str, uint8_t * buffer, uint32_t * data_offset)
{
    uint32_t result = 0;
    size_t length = strlen(str) + 1;
    if(length <= 4)
    {
        //we can fit in 4 bytes, so just pack the string into result
        memcpy(&result, str, length);
    }
    else
    {
        result = *data_offset;
        memcpy(buffer + result, str, length);
        *data_offset += length;
        //align to 2 bytes
        if(*data_offset % 2) *data_offset += 1;
    }
    return result;
}

static uint32_t add_rational(int32_t numerator, int32_t denominator, uint8_t * buffer, uint32_t * data_offset)
{
    uint32_t result = *data_offset;
    memcpy(buffer + *data_offset, &numerator, sizeof(int32_t));
    *data_offset += sizeof(int32_t);
    memcpy(buffer + *data_offset, &denominator, sizeof(int32_t));
    *data_offset += sizeof(int32_t);
    return result;
}

static inline uint8_t to_tc_byte(int value)
{
    return (((value / 10) << 4) | (value % 10));
}

static uint32_t add_timecode(double framerate, int frame, uint8_t * buffer, uint32_t * data_offset)
{
    uint32_t result = *data_offset;
    memset(buffer + *data_offset, 0, 8);
    int hours, minutes, seconds, frames;
    
    /*
    //use drop frame if the framerate is close to 30 / 1.001
    int drop_frame = round((1.0 - framerate / ceil(framerate)) * 1000) == 1 && ceil(framerate) == 30;
    
    if (drop_frame)
    {
        int d = frame / 17982;
        int m = frame % 17982;
        int f = frame + 18 * d + 2 * (MAX(0, m - 2) / 1798);
        
        hours = ((f / 30) / 60) / 60;
        minutes = ((f / 30) / 60) % 60;
        seconds = (f / 30) % 60;
        frames = f % 30;
    }
    */
    
    //round the framerate to the next largest integral framerate
    //tc will not match real world time if framerate is not an integer
    double time = framerate == 0 ? 0 : frame / (framerate > 1 ? round(framerate) : framerate);
    hours = (int)floor(time / 3600);
    minutes = ((int)floor(time / 60)) % 60;
    seconds = ((int)floor(time)) % 60;
    frames = framerate > 1 ? (frame % ((int)round(framerate))) : 0;
    
    buffer[*data_offset] = to_tc_byte(frames) & 0x3F;
    //if(drop_frame) buffer[*data_offset] = buffer[*data_offset] | (1 << 7); //set the drop frame bit
    buffer[*data_offset + 1] = to_tc_byte(seconds) & 0x7F;
    buffer[*data_offset + 2] = to_tc_byte(minutes) & 0x7F;
    buffer[*data_offset + 3] = to_tc_byte(hours) & 0x3F;
    
    *data_offset += 8;
    return result;
}

static void add_ifd(struct directory_entry * ifd, uint8_t * header, size_t * position, int count, uint32_t next_ifd_offset)
{
    *(uint16_t*)(header + *position) = count;
    *position += sizeof(uint16_t);
    memcpy(header + *position, ifd, count * sizeof(struct directory_entry));
    *position += count * sizeof(struct directory_entry);
    memcpy(header + *position, &next_ifd_offset, sizeof(uint32_t));
    *position += sizeof(uint32_t);
}

static char * format_datetime(char * datetime,
                              size_t datetime_capacity,
                              mlvObject_t * mlv_data,
                              uint32_t frame_index)
{
    /* VIDF is mutable decode scratch and cache workers can advance it while a
     * different frame is exported.  Bind DateTime to the indexed target frame
     * instead, which is immutable after clip indexing. */
    const uint64_t frame_timestamp = mlv_data->video_index[frame_index].frame_time;
    /* MCRAW currently indexes milliseconds and CinemaDNG folders use a
     * synthetic ordinal. Their historical DateTime behavior is clip-local
     * RTCI with no frame offset, so preserve it until those formats expose a
     * canonical microsecond timestamp. */
    const int source_has_mlv_microsecond_timestamps =
        (mlv_data->MLVI.videoClass & (MLV_VIDEO_CLASS_FLAG_MCRAW
                                      | MLV_VIDEO_CLASS_FLAG_DNGSEQ)) == 0;
    const uint64_t elapsed_seconds = source_has_mlv_microsecond_timestamps
        && frame_timestamp >= mlv_data->RTCI.timestamp
            ? (frame_timestamp - mlv_data->RTCI.timestamp) / 1000000u
            : 0u;
    const uint64_t seconds = (uint64_t)mlv_data->RTCI.tm_sec + elapsed_seconds;
    const uint64_t minutes = (uint64_t)mlv_data->RTCI.tm_min + seconds / 60u;
    const uint64_t hours = (uint64_t)mlv_data->RTCI.tm_hour + minutes / 60u;
    const uint64_t days = (uint64_t)mlv_data->RTCI.tm_mday + hours / 24u;
    //TODO: days could also overflow in the month, but this is no longer simple modulo arithmetic like with hr:min:sec
    snprintf(datetime,
             datetime_capacity,
             "%04d:%02d:%02llu %02llu:%02llu:%02llu",
             1900 + mlv_data->RTCI.tm_year,
             mlv_data->RTCI.tm_mon + 1,
             (unsigned long long)days,
             (unsigned long long)(hours % 24u),
             (unsigned long long)(minutes % 60u),
             (unsigned long long)(seconds % 60u));
    return datetime;
}

int dngFormatDateTimeForFrameForTesting(mlvObject_t * mlv_data,
                                        uint32_t frame_index,
                                        char * datetime,
                                        size_t datetime_capacity)
{
    if(!mlv_data || !mlv_data->video_index || !datetime
       || datetime_capacity == 0 || frame_index >= mlv_data->frames) {
        return 0;
    }

    format_datetime(datetime, datetime_capacity, mlv_data, frame_index);
    return 1;
}

/* returns the size of uncompressed image data. does not include header */
static size_t dng_get_image_size(mlvObject_t * mlv_data, int size_mode, uint64_t frame_index)
{
    if(!mlv_data) return 0;
    switch(size_mode)
    {
        case IMG_SIZE_PACKED:
        {
            const uint32_t bpp = mlv_data->RAWI.raw_info.bits_per_pixel;
            if(mlv_data->RAWI.xRes == 0 || mlv_data->RAWI.yRes == 0
               || bpp == 0 || bpp > 16
               || (size_t)mlv_data->RAWI.xRes > SIZE_MAX / (size_t)mlv_data->RAWI.yRes)
                return 0;
            const size_t pixels = (size_t)mlv_data->RAWI.xRes * (size_t)mlv_data->RAWI.yRes;
            if(pixels > SIZE_MAX / (size_t)bpp) return 0;
            const size_t bits = pixels * (size_t)bpp;
            /* The legacy pack/unpack API exposes uint16_t words rather than a
             * byte capacity. Refuse a partial final word so every caller and
             * the bit helpers agree on the exact writable extent. */
            return (bits & 15u) == 0 ? bits / 8u : 0;
        }
            break;
        case IMG_SIZE_LOSLESS:
            return mlv_data->video_index[frame_index].frame_size;
            break;
        case IMG_SIZE_UNPACKED:
        default:
            if(mlv_data->RAWI.xRes == 0 || mlv_data->RAWI.yRes == 0
               || (size_t)mlv_data->RAWI.xRes > SIZE_MAX / (size_t)mlv_data->RAWI.yRes)
                return 0;
            {
                const size_t pixels = (size_t)mlv_data->RAWI.xRes * (size_t)mlv_data->RAWI.yRes;
                return pixels <= SIZE_MAX / sizeof(uint16_t)
                    ? pixels * sizeof(uint16_t) : 0;
            }
            break;
    }
}

int loadDngPropertiesString(char *props_buffer, char *key, char *value, int max_size)
{
    if (props_buffer == NULL) {
        return -1;
    }

    char *p = strstr(props_buffer, key);
    char *d = value;

    int len = 0;

    if (p != NULL)
    {
        p += strlen(key) + 2;

        while (*p != 0 && *p != '\n' && *p != '\r' && len < max_size - 1)
        {
            *d++ = *p++;
            len++;
        }
    }

    *d = 0;

    return len;
}

int loadDngPropertiesInt(char *props_buffer, char *key, int *value)
{
    if (props_buffer == NULL) {
        return -1;
    }

    int res = 0;

    char *p = strstr(props_buffer, key);

    if (p != NULL)
    {
        p += strlen(key) + 2;

        res = atoi(p);
    }

    *value = res;

    return res;
}

/* generates the CDNG header. The result is written into dng_data struct */
static void dng_fill_header(mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, const char *props_filename)
{
    uint8_t * header = dng_data->header_buf;
    size_t position = 0;
    if(header)
    {
        memset(header, 0 , HEADER_SIZE);
        memcpy(header + position, tiff_header, sizeof(tiff_header));
        position += sizeof(tiff_header);
        
        uint32_t exif_ifd_offset = (uint32_t)(position + sizeof(uint16_t) + IFD0_COUNT * sizeof(struct directory_entry) + sizeof(uint32_t));
        uint32_t data_offset = exif_ifd_offset + sizeof(uint16_t) + EXIF_IFD_COUNT * sizeof(struct directory_entry) + sizeof(uint32_t);

        camera_id_t *camid = camidGet(mlv_data->IDNT.cameraModel);

        /* Use camid from mlv_data if current cam is default */
        if (!camid->cameraModel) {
            camid = &mlv_data->camid;
        }

        char *props_buffer = NULL;

        if (props_filename != NULL)
        {
            FILE *fd = fopen(props_filename, "rb");

            if (fd != NULL)
            {
                fseek(fd, 0, SEEK_END);
                long file_size = ftell(fd);
                fseek(fd, 0, SEEK_SET);

                props_buffer = (char*)malloc(file_size + 1);
                fread(props_buffer, file_size, 1, fd);
                props_buffer[file_size] = '\0';
                fclose(fd);
            }
        }

        /* 'Make' Tag */
        char make[33] = { 0 };
        memcpy(make, mlv_data->IDNT.cameraName, 32);
        char * space = strchr(make, ' ');
        if(space) *space = 0x0;

        /* 'Camera Model Name' Tag */
        char model[33] = { 0 };
        memcpy(model, mlv_data->IDNT.cameraName, 32);

        /* 'Camera Serial Number' Tag */
        char serial[33] = { 0 };
        memcpy(serial, mlv_data->IDNT.cameraSerial, 32);
        
        /* 'Unique Camera Model' Tag */
        const char * unique_model = camid->cameraName[UNIQ];

        char unique_model2[64] = "";
        if (loadDngPropertiesString(props_buffer, "UniqueCameraModel", unique_model2, 64) > 0) {
            unique_model = unique_model2;
        }

        int black_level = mlv_data->llrawproc->dng_black_level;
        loadDngPropertiesInt(props_buffer, "BlackLevel", &black_level);

        int white_level = mlv_data->llrawproc->dng_white_level;
        loadDngPropertiesInt(props_buffer, "WhiteLevel", &white_level);

        if(dng_data->overrides.enabled)
        {
            if(dng_data->overrides.black_level_enabled)
            {
                black_level = dng_data->overrides.black_level;
            }
            if(dng_data->overrides.white_level_enabled)
            {
                white_level = dng_data->overrides.white_level;
            }
        }

        /* Focal resolution stuff. Work on local copies; the sampling/aspect
           adjustments below must not mutate the shared camera database. */
        int32_t focal_resolution_x[2] = { camid->focal_resolution_x[0], camid->focal_resolution_x[1] };
        int32_t focal_resolution_y[2] = { camid->focal_resolution_y[0], camid->focal_resolution_y[1] };

        /* Picture aspect ratio */
        int manual_ar = 0;
        int32_t pic_ar[4] = {1,1,1,1};
        int32_t * par = NULL;
        if(dng_data->par[0])
        {
#ifndef STDOUT_SILENT
            //printf("cDNG: manual aspect ratio used\n");
#endif
            manual_ar = 1;
            par = dng_data->par;
        }
        else
        {
#ifndef STDOUT_SILENT
            printf("cDNG: auto detected aspect ratio used\n");
#endif
            par = pic_ar;
        }

        /* If RAWC block present calculate AR and FR from binning/skipping values */
        if(mlv_data->RAWC.blockType[0])
        {
            int sampling_x = mlv_data->RAWC.binning_x + mlv_data->RAWC.skipping_x;
            int sampling_y = mlv_data->RAWC.binning_y + mlv_data->RAWC.skipping_y;

            if(!manual_ar)
            {
                par[2] = sampling_y; par[3] = sampling_x;
            }

            focal_resolution_x[1] = focal_resolution_x[1] * sampling_x;
            focal_resolution_y[1] = focal_resolution_y[1] * sampling_y;
        }
        else // use old method to calculate aspect ratio and detect crop_rec
        {
            double rawW = mlv_data->RAWI.raw_info.active_area.x2 - mlv_data->RAWI.raw_info.active_area.x1;
            double rawH = mlv_data->RAWI.raw_info.active_area.y2 - mlv_data->RAWI.raw_info.active_area.y1;
            double aspect_ratio = rawW / rawH;
            //check the aspect ratio of the original raw buffer, if it's > 2 and we're not in crop mode, then this is probably squeezed footage
            if(aspect_ratio > 2.0 && rawH <= 720 && llrpDetectFocusDotFixMode(mlv_data) != 2)
            {
                if(!manual_ar)
                {
                    // 5x3 line skpping
                    par[2] = 5; par[3] = 3;
                }

                focal_resolution_x[1] = focal_resolution_x[1] * 3;
                focal_resolution_y[1] = focal_resolution_y[1] * 5;
            }
            //if the width is larger than 2000, we're probably not in crop mode
            else if(rawW < 2000)
            {
                focal_resolution_x[1] = focal_resolution_x[1] * 3;
                focal_resolution_y[1] = focal_resolution_y[1] * 3;
            }
        }

        //we get the active area of the original raw source, not the recorded data, so overwrite the active area if the recorded data does
        //not contain the OB areas
        if(mlv_data->RAWI.xRes < mlv_data->RAWI.raw_info.active_area.x2 ||
           mlv_data->RAWI.yRes < mlv_data->RAWI.raw_info.active_area.y2)
        {
            mlv_data->RAWI.raw_info.active_area.x1 = 0;
            mlv_data->RAWI.raw_info.active_area.y1 = 0;
            mlv_data->RAWI.raw_info.active_area.x2 = mlv_data->RAWI.xRes;
            mlv_data->RAWI.raw_info.active_area.y2 = mlv_data->RAWI.yRes;
        }
        
        /* FPS stuff*/
        int32_t frame_rate[2] = {mlv_data->MLVI.sourceFpsNom, mlv_data->MLVI.sourceFpsDenom};
        if(dng_data->fps_float > 0)
        {
            frame_rate[0] = (int32_t)(dng_data->fps_float * 1000);
            frame_rate[1] = 1000;
        }
        double frame_rate_f = frame_rate[1] == 0 ? 0 : (double)frame_rate[0] / (double)frame_rate[1];
        
        /* Date */
        char datetime[255];

        /* Baseline exposure stuff */
        int32_t basline_exposure[2] = {mlv_data->RAWI.raw_info.exposure_bias[0],mlv_data->RAWI.raw_info.exposure_bias[1]};
        if(basline_exposure[1] == 0)
        {
            basline_exposure[0] = 0;
            basline_exposure[1] = 1;
        }
        if(dng_data->overrides.enabled
        && dng_data->overrides.baseline_exposure_enabled)
        {
            dng_add_srational(basline_exposure,
                              dng_data->overrides.baseline_exposure);
        }

        int32_t dng_active_area[4] = {
            mlv_data->RAWI.raw_info.dng_active_area[0],
            mlv_data->RAWI.raw_info.dng_active_area[1],
            mlv_data->RAWI.raw_info.dng_active_area[2],
            mlv_data->RAWI.raw_info.dng_active_area[3]
        };
        if(dng_active_area[0] < 0 || dng_active_area[1] < 0
           || dng_active_area[2] <= dng_active_area[0]
           || dng_active_area[3] <= dng_active_area[1]
           || (uint32_t)dng_active_area[2] > mlv_data->RAWI.yRes
           || (uint32_t)dng_active_area[3] > mlv_data->RAWI.xRes)
        {
            dng_active_area[0] = 0;
            dng_active_area[1] = 0;
            dng_active_area[2] = mlv_data->RAWI.yRes;
            dng_active_area[3] = mlv_data->RAWI.xRes;
        }
        const int32_t active_width = dng_active_area[3] - dng_active_area[1];
        const int32_t active_height = dng_active_area[2] - dng_active_area[0];
        int32_t default_crop_origin[2] = {
            mlv_data->RAWI.raw_info.crop.origin[0],
            mlv_data->RAWI.raw_info.crop.origin[1]
        };
        int32_t default_crop_size[2] = {
            mlv_data->RAWI.raw_info.crop.size[0],
            mlv_data->RAWI.raw_info.crop.size[1]
        };
        if(default_crop_origin[0] < 0 || default_crop_origin[1] < 0
           || default_crop_size[0] <= 0 || default_crop_size[1] <= 0
           || default_crop_origin[0] > UINT16_MAX
           || default_crop_origin[1] > UINT16_MAX
           || default_crop_size[0] > UINT16_MAX
           || default_crop_size[1] > UINT16_MAX
           || (int64_t)default_crop_origin[0] + default_crop_size[0] > active_width
           || (int64_t)default_crop_origin[1] + default_crop_size[1] > active_height)
        {
            default_crop_origin[0] = 0;
            default_crop_origin[1] = 0;
            default_crop_size[0] = active_width;
            default_crop_size[1] = active_height;
        }

        /* Time code stuff */
        //number of frames since midnight
        int tc_frame = (int)mlv_data->video_index[frame_index].frame_number;// + (uint64_t)((mlv_data->RTCI.tm_hour * 3600 + mlv_data->RTCI.tm_min * 60 + mlv_data->RTCI.tm_sec) * mlv_data->MLVI.sourceFpsNom) / (uint64_t)mlv_data->MLVI.sourceFpsDenom;
        
        /* White balance stuff */
        int32_t wbal[6];
        get_white_balance(mlv_data->WBAL, wbal, mlv_data->IDNT.cameraModel);
        if(dng_data->overrides.enabled
        && dng_data->overrides.as_shot_neutral_enabled)
        {
            memcpy(wbal,
                   dng_data->overrides.as_shot_neutral,
                   sizeof(wbal));
        }

        uint32_t cfa_pattern = mlv_data->RAWI.raw_info.cfa_pattern;
        if (cfa_pattern == 0) {
            cfa_pattern = 0x02010100;   // RGGB
        }

        /* tcReelName — pick the basename after the last path separator.
         * Handle both '/' and '\\' on every platform because Qt / CLI
         * paths on Windows often arrive with forward slashes. If we only
         * checked one separator the wrong flavour leaked the whole
         * absolute path into tcReelName and made DNG output
         * host-path-dependent — see
         * .claude/profiling/20260422-clipgolden-investigation/findings.md.
         */
        char * last_fwd = strrchr(mlv_data->path, '/');
        char * last_bck = strrchr(mlv_data->path, '\\');
        char * reel_name = (last_fwd > last_bck) ? last_fwd : last_bck;
        (!reel_name) ? (reel_name = mlv_data->path) : ++reel_name;
        char * ext_dot = strrchr(reel_name, '.');
        if(ext_dot) *ext_dot = '\000';


        /* Fill up IFD structs */
        struct directory_entry IFD0[IFD0_COUNT] =
        {
            {tcNewSubFileType,              ttLong,     1,      sfMainImage},
            {tcImageWidth,                  ttLong,     1,      mlv_data->RAWI.xRes},
            {tcImageLength,                 ttLong,     1,      mlv_data->RAWI.yRes},
            {tcBitsPerSample,               ttShort,    1,      mlv_data->llrawproc->dng_bit_depth},
            {tcCompression,                 ttShort,    1,      (!(dng_data->raw_output_state % 2)) ? ccUncompressed : ccJPEG},
            {tcPhotometricInterpretation,   ttShort,    1,      piCFA},
            {tcFillOrder,                   ttShort,    1,      1},
            {tcMake,                        ttAscii,    STRING_ENTRY(make, header, &data_offset)},
            {tcModel,                       ttAscii,    STRING_ENTRY(model, header, &data_offset)},
            {tcStripOffsets,                ttLong,     1,      (uint32_t)HEADER_SIZE},
            {tcOrientation,                 ttShort,    1,      1},
            {tcSamplesPerPixel,             ttShort,    1,      1},
            {tcRowsPerStrip,                ttShort,    1,      mlv_data->RAWI.yRes},
            {tcStripByteCounts,             ttLong,     1,      dng_data->image_size},
            {tcPlanarConfiguration,         ttShort,    1,      pcInterleaved},
            {tcSoftware,                    ttAscii,    STRING_ENTRY(SOFTWARE_NAME, header, &data_offset)},
            {tcDateTime,                    ttAscii,    STRING_ENTRY(format_datetime(datetime, sizeof(datetime), mlv_data, frame_index), header, &data_offset)},
            {tcCFARepeatPatternDim,         ttShort,    2,      0x00020002}, //2x2
            {tcCFAPattern,                  ttByte,     4,      cfa_pattern},
            {tcExifIFD,                     ttLong,     1,      exif_ifd_offset},
            {tcDNGVersion,                  ttByte,     4,      0x00000401}, //1.4.0.0 in little endian
            {tcUniqueCameraModel,           ttAscii,    STRING_ENTRY(unique_model, header, &data_offset)},
            {tcBlackLevel,                  ttLong,     1,      black_level},
            {tcWhiteLevel,                  ttLong,     1,      white_level},
            {tcDefaultScale,                ttRational, RATIONAL_ENTRY(par, header, &data_offset, 4)},
            {tcDefaultCropOrigin,           ttShort,    2,      PACK(default_crop_origin)},
            {tcDefaultCropSize,             ttShort,    2,      PACK(default_crop_size)},
            {tcColorMatrix1,                ttSRational,RATIONAL_ENTRY(camid->ColorMatrix1, header, &data_offset, 18)},
            {tcColorMatrix2,                ttSRational,RATIONAL_ENTRY(camid->ColorMatrix2, header, &data_offset, 18)},
            {tcAsShotNeutral,               ttRational, RATIONAL_ENTRY(wbal, header, &data_offset, 6)},
            {tcBaselineExposure,            ttSRational,RATIONAL_ENTRY(basline_exposure, header, &data_offset, 2)},
            {tcCameraSerialNumber,          ttAscii,    STRING_ENTRY(serial, header, &data_offset)},
            {tcCalibrationIlluminant1,      ttShort,    1,      lsStandardLightA},
            {tcCalibrationIlluminant2,      ttShort,    1,      lsD65},
            {tcActiveArea,                  ttLong,     ARRAY_ENTRY(dng_active_area, header, &data_offset, 4)},
            {tcForwardMatrix1,              ttSRational,RATIONAL_ENTRY(camid->ForwardMatrix1, header, &data_offset, 18)},
            {tcForwardMatrix2,              ttSRational,RATIONAL_ENTRY(camid->ForwardMatrix2, header, &data_offset, 18)},
            {tcTimeCodes,                   ttByte,     8,      add_timecode(frame_rate_f, tc_frame, header, &data_offset)},
            {tcFrameRate,                   ttSRational,RATIONAL_ENTRY(frame_rate, header, &data_offset, 2)},
            {tcReelName,                    ttAscii,    STRING_ENTRY(reel_name, header, &data_offset)},
            {tcBaselineExposureOffset,      ttSRational,RATIONAL_ENTRY2(0, 1, header, &data_offset)},
        };
        
        struct directory_entry EXIF_IFD[EXIF_IFD_COUNT] =
        {
            {tcExposureTime,                ttRational, RATIONAL_ENTRY2((int32_t)mlv_data->EXPO.shutterValue, 1000000, header, &data_offset)},
            {tcFNumber,                     ttRational, RATIONAL_ENTRY2(mlv_data->LENS.aperture, 100, header, &data_offset)},
            {tcISOSpeedRatings,             ttShort,    1,      mlv_data->EXPO.isoValue},
            {tcSensitivityType,             ttShort,    1,      stISOSpeed},
            {tcExifVersion,                 ttUndefined,4,      0x30333230},
            {tcSubjectDistance,             ttRational, RATIONAL_ENTRY2(mlv_data->LENS.focalDist, 1, header, &data_offset)},
            {tcFocalLength,                 ttRational, RATIONAL_ENTRY2(mlv_data->LENS.focalLength, 1, header, &data_offset)},
            {tcFocalPlaneXResolutionExif,   ttRational, RATIONAL_ENTRY(focal_resolution_x, header, &data_offset, 2)},
            {tcFocalPlaneYResolutionExif,   ttRational, RATIONAL_ENTRY(focal_resolution_y, header, &data_offset, 2)},
            {tcFocalPlaneResolutionUnitExif,ttShort,    1,      camid->focal_unit}, //inches
            {tcLensModelExif,               ttAscii,    STRING_ENTRY((char*)mlv_data->LENS.lensName, header, &data_offset)},
        };
        
        /* update the StripOffsets to the correct location
           the image data starts where our extra data ends */
        IFD0[9].value = data_offset;

        add_ifd(IFD0, header, &position, IFD0_COUNT, 0);
        add_ifd(EXIF_IFD, header, &position, EXIF_IFD_COUNT, 0);
        
        /* set real header size */
        dng_data->header_size = data_offset;

        if (props_buffer != NULL) {
            free(props_buffer);
        }
    }
}

/* unpack bits to 16 bit little endian and converts to real 14bit if less then 14bit depth detected
   output_buffer - the buffer where the result will be written
   input_buffer - a buffer containing the packed imaged data
   width - image width
   height - image height
   bpp - raw data bits per pixel
*/
void dng_unpack_image_bits(uint16_t * output_buffer, uint16_t * input_buffer, int width, int height, uint32_t bpp)
{
    if (!output_buffer || !input_buffer || width <= 0 || height <= 0 || bpp == 0 || bpp > 16
        || (size_t)width > SIZE_MAX / (size_t)height) {
        return;
    }
    const size_t pixel_count = (size_t)width * (size_t)height;
    if (pixel_count > SIZE_MAX / (size_t)bpp) {
        return;
    }
    const size_t packed_words = (pixel_count * (size_t)bpp + 15u) / 16u;
    uint32_t mask = bpp == 16u ? UINT16_MAX : ((UINT32_C(1) << bpp) - 1u);
    uint16_t *packed_bits = input_buffer;
    uint16_t *unpacked_bits = output_buffer;

    #pragma omp parallel for
    for (size_t pixel_index = 0; pixel_index < pixel_count; pixel_index++)
    {
        const size_t bits_offset = pixel_index * (size_t)bpp;
        const size_t bits_address = bits_offset / 16u;
        const uint32_t bits_shift = (uint32_t)(bits_offset % 16u);

        const uint32_t first_bits = 16u - bits_shift;
        uint32_t data;
        if (first_bits >= bpp) {
            data = (uint32_t)packed_bits[bits_address] >> (first_bits - bpp);
        } else {
            const uint32_t remaining_bits = bpp - first_bits;
            const uint32_t low_part = (uint32_t)packed_bits[bits_address]
                & ((UINT32_C(1) << first_bits) - 1u);
            const uint32_t high_part = bits_address + 1u < packed_words
                ? (uint32_t)packed_bits[bits_address + 1u] >> (16u - remaining_bits)
                : 0u;
            data = (low_part << remaining_bits) | high_part;
        }
        unpacked_bits[pixel_index] = (uint16_t)(data & mask);
    }
}

/* pack bits to 16 bit little endian and convert to big endian (raw payload DNG spec)
   output_buffer - the buffer where the result will be written
   input_buffer - a buffer containing the unpacked imaged data
   width - image width
   height - image height
   bpp - raw data bits per pixel 
*/
void dng_pack_image_bits(uint16_t * output_buffer, uint16_t * input_buffer, int width, int height, uint32_t bpp, int big_endian)
{
    if (!output_buffer || !input_buffer || width <= 0 || height <= 0 || bpp == 0 || bpp > 16
        || (size_t)width > SIZE_MAX / (size_t)height) {
        return;
    }
    const size_t pixel_count = (size_t)width * (size_t)height;
    if (pixel_count > SIZE_MAX / (size_t)bpp) {
        return;
    }
    const size_t total_bits = pixel_count * (size_t)bpp;
    /* All production callers retain 16-bit-word packed payloads. Refuse an
     * odd trailing byte because this legacy pointer-only API has no capacity
     * parameter with which to prove that a final full word is writable. */
    if ((total_bits & 15u) != 0) {
        return;
    }
    const size_t packed_words = (total_bits + 15u) / 16u;
    const uint32_t mask = bpp == 16u ? UINT16_MAX : ((UINT32_C(1) << bpp) - 1u);
    uint16_t *unpacked_bits = input_buffer;
    uint16_t *packed_bits = output_buffer;

    memset(packed_bits, 0, packed_words * sizeof(*packed_bits));
    for (size_t pixel_index = 0; pixel_index < pixel_count; pixel_index++)
    {
        const size_t bits_offset = pixel_index * (size_t)bpp;
        const size_t bits_address = bits_offset / 16u;
        const uint32_t bits_shift = (uint32_t)(bits_offset % 16u);
        const uint32_t first_bits = 16u - bits_shift;
        const uint32_t data = (uint32_t)unpacked_bits[pixel_index] & mask;

        if (first_bits >= bpp) {
            packed_bits[bits_address] = (uint16_t)(packed_bits[bits_address]
                | (uint16_t)(data << (first_bits - bpp)));
        } else {
            const uint32_t remaining_bits = bpp - first_bits;
            packed_bits[bits_address] = (uint16_t)(packed_bits[bits_address]
                | (uint16_t)(data >> remaining_bits));
            if (bits_address + 1u < packed_words) {
                packed_bits[bits_address + 1u] = (uint16_t)(packed_bits[bits_address + 1u]
                    | (uint16_t)(data << (16u - remaining_bits)));
            }
        }
    }

    if (big_endian) {
        for (size_t word_index = 0; word_index < packed_words; ++word_index) {
            packed_bits[word_index] = ROL16(packed_bits[word_index], 8);
        }
    }
}

/* decompress LJ92 image to output_buffer */
int dng_decompress_image(uint16_t * output_buffer, size_t output_buffer_capacity, uint16_t * input_buffer, size_t input_buffer_size, int width, int height, uint32_t bpp)
{
    if (!output_buffer || !input_buffer || input_buffer_size == 0 || input_buffer_size > (size_t)INT_MAX
        || width <= 0 || height <= 0
        || (size_t)width > SIZE_MAX / (size_t)height
        || (size_t)width * (size_t)height > SIZE_MAX / sizeof(uint16_t)) {
        return LJ92_ERROR_CORRUPT;
    }
    const size_t expected_pixels = (size_t)width * (size_t)height;
    const size_t expected_bytes = expected_pixels * sizeof(uint16_t);
    if (output_buffer_capacity < expected_bytes || bpp == 0 || bpp > 16) {
        return LJ92_ERROR_CORRUPT;
    }
    int components = 1;
    int decoded_bpp = 0;
    lj92 decoder_object;

    const uint32_t expected_bpp = bpp;
    int ret = lj92_open(&decoder_object, (uint8_t*)input_buffer, input_buffer_size, &width, &height, &decoded_bpp, &components);
    if(ret != LJ92_ERROR_NONE)
    {
#ifndef STDOUT_SILENT
        printf("LJ92 decoder: Failed with error code (%d)\n", ret);
#endif
        memset(output_buffer, 0, expected_bytes);
        return ret;
    }

    size_t decoded_pixels = 0;
    if (width <= 0 || height <= 0 || components <= 0
        || (size_t)width > SIZE_MAX / (size_t)height
        || (size_t)width * (size_t)height > SIZE_MAX / (size_t)components) {
        ret = LJ92_ERROR_CORRUPT;
    } else {
        decoded_pixels = (size_t)width * (size_t)height * (size_t)components;
    }
    /* Native output is one component. Retain compatibility with valid
     * two-component lossless JPEGs only when their decoded sample total and
     * bit depth match the caller's retained RAW geometry exactly. */
    if (ret == LJ92_ERROR_NONE
        && ((components != 1 && components != 2) || decoded_bpp != (int)expected_bpp
            || decoded_pixels != expected_pixels || expected_pixels > (size_t)INT_MAX)) {
        ret = LJ92_ERROR_CORRUPT;
    }
    if (ret == LJ92_ERROR_NONE) {
        ret = lj92_decode(decoder_object, output_buffer, (int)expected_pixels, 0, NULL, 0);
    }
    if(ret != LJ92_ERROR_NONE)
    {
#ifndef STDOUT_SILENT
        printf("LJ92 decoder: Failed with error code (%d)\n", ret);
#endif
        memset(output_buffer, 0, expected_bytes);
    }

    lj92_close(decoder_object);
    return ret;
}

int dng_lj92_output_capacity(int width, int height, size_t * output_capacity)
{
    if (!output_capacity || width <= 0 || height <= 1 || (height & 1) != 0
        || width > UINT16_MAX / 2 || height / 2 > UINT16_MAX
        || (size_t)width > SIZE_MAX / (size_t)height) {
        return 0;
    }
    const size_t input_pixels = (size_t)width * (size_t)height;
    if (input_pixels > (SIZE_MAX - 200u) / 3u) {
        return 0;
    }
    const int encoded_width = width * 2;
    const int encoded_height = height / 2;
    if (encoded_height <= 0 || encoded_width > INT_MAX / encoded_height
        || encoded_width * encoded_height > (INT_MAX - 200) / 3) {
        return 0;
    }
    *output_capacity = input_pixels * 3u + 200u;
    return 1;
}

static int dng_compress_image_profiled(uint16_t * output_buffer,
                                       size_t output_buffer_capacity,
                                       uint16_t * input_buffer,
                                       size_t * output_buffer_size,
                                       int width,
                                       int height,
                                       uint32_t bpp,
                                       exportProfileFrame_t * profile_frame)
{
    if (!output_buffer || !input_buffer || !output_buffer_size
        || width <= 0 || height <= 0 || bpp == 0 || bpp > 16 || width > INT_MAX / 2
        || (size_t)width > SIZE_MAX / (size_t)height
        || (size_t)width * (size_t)height > SIZE_MAX / sizeof(uint16_t)) {
        if (output_buffer_size) *output_buffer_size = 0;
        return LJ92_ERROR_CORRUPT;
    }
    size_t maximum_encoded_size = 0;
    if (!dng_lj92_output_capacity(width, height, &maximum_encoded_size)
        || output_buffer_capacity == 0) {
        *output_buffer_size = 0;
        return LJ92_ERROR_CORRUPT;
    }
    uint8_t * compressed = NULL;
    int new_width = width * 2;
    int new_height = height / 2;

    double profile_stage_start = export_profile_stage_begin(profile_frame);
    if (new_height <= 0 || new_width > INT_MAX / new_height) {
        return LJ92_ERROR_CORRUPT;
    }
    int encoded_length = 0;
    int ret = lj92_encode(input_buffer, new_width, new_height, (int)bpp,
                          new_width * new_height, 0, NULL, 0,
                          &compressed, &encoded_length);
    if (ret == LJ92_ERROR_NONE
        && (!compressed || encoded_length < 0
            || (size_t)encoded_length > maximum_encoded_size
            || (size_t)encoded_length > output_buffer_capacity)) {
        ret = LJ92_ERROR_CORRUPT;
    }
    export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_COMPRESS_ENCODE, profile_stage_start);

    if(ret == LJ92_ERROR_NONE)
    {
        *output_buffer_size = (size_t)encoded_length;
        profile_stage_start = export_profile_stage_begin(profile_frame);
        memcpy(output_buffer, compressed, *output_buffer_size);
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_COMPRESS_COPY, profile_stage_start);
#ifndef STDOUT_SILENT
        size_t input_buffer_size = (size_t)width * (size_t)height * sizeof(uint16_t);
        printf("LJ92 encoder: "FMT_SIZE" -> "FMT_SIZE" (%2.2f%% ratio)\n", *output_buffer_size, input_buffer_size, ((float)*output_buffer_size * 100.0f) / (float)input_buffer_size);
#endif
    }
    else
    {
        *output_buffer_size = 0;
#ifndef STDOUT_SILENT
        printf("LJ92 encoder: failed with error code (%d)\n", ret);
#endif
    }

    if(compressed)
    {
        profile_stage_start = export_profile_stage_begin(profile_frame);
        free(compressed);
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_COMPRESS_CLEANUP, profile_stage_start);
    }

    return ret;
}

/* compress input_buffer to LJ92 image */
int dng_compress_image(uint16_t * output_buffer, size_t output_buffer_capacity, uint16_t * input_buffer, size_t * output_buffer_size, int width, int height, uint32_t bpp)
{
    return dng_compress_image_profiled(output_buffer,
                                      output_buffer_capacity,
                                      input_buffer,
                                      output_buffer_size,
                                      width,
                                      height,
                                      bpp,
                                      NULL);
}

/* changes endianness of the 16 bit buffer values
   DNG spec: 10/12/14bit raw should be big endian, 8/16/32bit raw can be little endian
   input_buffer - pointer to the buffer
   buf_size - the size of the buffer in bytes
*/
static void dng_reverse_byte_order(uint16_t * input_buffer, size_t buf_size)
{
    uint32_t index_max = buf_size / 2;

    #pragma omp parallel for
    for (uint32_t index = 0; index < index_max; index++)
    {
        input_buffer[index] = ROL16(input_buffer[index], 8);
    }
}

static void dng_reset_deferred_payload_compress(dngObject_t * dng_data)
{
    if(!dng_data) return;
    dng_data->defer_payload_compress = 0;
    dng_data->payload_compress_width = 0;
    dng_data->payload_compress_height = 0;
    dng_data->payload_compress_bpp = 0;
    dng_data->payload_compress_input_bytes = 0;
}

static int dng_frame_request_is_valid(const mlvObject_t * mlv_data,
                                      const dngObject_t * dng_data,
                                      uint32_t frame_index)
{
    if(!mlv_data || !dng_data || !mlv_data->video_index
       || frame_index >= mlv_data->frames) {
        return 0;
    }
    if(isDngFolderLoaded(mlv_data)) return 1;
    if(!mlv_data->file) return 0;

    const uint16_t chunk_num = mlv_data->video_index[frame_index].chunk_num;
    if(isMcrawLoaded(mlv_data)) {
        return chunk_num == 0 && mlv_data->file[0];
    }
    return mlv_data->filenum > 0 && (int)chunk_num < mlv_data->filenum
        && mlv_data->file[chunk_num];
}

static int dng_defer_compress_to_payload(dngObject_t * dng_data,
                                         mlvObject_t * mlv_data,
                                         uint32_t frame_index,
                                         uint32_t bpp)
{
    if(!dng_data || !mlv_data) return 1;
    if(!dng_data->image_buf || !dng_data->image_buf_unpacked) return 1;

    uint16_t * ready_image = dng_data->image_buf_unpacked;
    dng_data->image_buf_unpacked = dng_data->image_buf;
    dng_data->image_buf = ready_image;
    dng_data->image_size =
        dng_get_image_size(mlv_data, IMG_SIZE_UNPACKED, frame_index);
    dng_data->defer_payload_compress = 1;
    dng_data->payload_compress_width = mlv_data->RAWI.xRes;
    dng_data->payload_compress_height = mlv_data->RAWI.yRes;
    dng_data->payload_compress_bpp = bpp;
    dng_data->payload_compress_input_bytes =
        dng_compress_input_bytes_for_frame(mlv_data);
    export_profile_note_async_writer_compress_used();
    return 0;
}

static int dng_prepare_compressed_frame(mlvObject_t * mlv_data,
                                        dngObject_t * dng_data,
                                        uint32_t frame_index,
                                        uint32_t bpp,
                                        int defer_compress_to_payload,
                                        exportProfileFrame_t * profile_frame)
{
    if(defer_compress_to_payload)
    {
        return dng_defer_compress_to_payload(dng_data,
                                             mlv_data,
                                             frame_index,
                                             bpp);
    }

    double profile_stage_start = export_profile_stage_begin(profile_frame);
    const int ret = dng_compress_image_profiled(dng_data->image_buf,
                                                dng_data->image_capacity,
                                                dng_data->image_buf_unpacked,
                                                &dng_data->image_size,
                                                mlv_data->RAWI.xRes,
                                                mlv_data->RAWI.yRes,
                                                bpp,
                                                profile_frame);
    export_profile_stage_end(profile_frame,
                             EXPORT_PROFILE_DNG_COMPRESS,
                             profile_stage_start);
    export_profile_note_dng_compress_bytes(
        profile_frame,
        dng_compress_input_bytes_for_frame(mlv_data),
        (uint64_t)dng_data->image_size,
        ret == 0);
    return ret;
}

/* build whole DNG frame (header + image), process image if needed and put to the dng struct ready to save */
static int dng_get_frame(mlvObject_t * mlv_data,
                         dngObject_t * dng_data,
                         uint32_t frame_index,
                         const char *prop_filename,
                         exportProfileFrame_t * profile_frame,
                         int defer_compress_to_payload)
{
    if(!dng_frame_request_is_valid(mlv_data, dng_data, frame_index)) return 1;

    int ret = 0;
    double profile_stage_start = 0.0;
    dng_reset_deferred_payload_compress(dng_data);
    llrpResetDngBWLevels(mlv_data);

    /* A CinemaDNG-folder clip has no backing MLV FILE*. Decode it through the
     * folder reader, then enter the same llrawproc + pack/compress boundary as
     * other inputs. This also makes folder-open -> DNG re-export a real
     * supported path instead of falling through to file_set_pos(NULL, ...). */
    if(isDngFolderLoaded(mlv_data))
    {
        profile_stage_start = export_profile_stage_begin(profile_frame);
        if(getMlvRawFrameUint16(mlv_data, frame_index,
                                dng_data->image_buf_unpacked) != 0)
        {
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_DECODE,
                                     profile_stage_start);
            return LJ92_ERROR_CORRUPT;
        }
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_DECODE,
                                 profile_stage_start);

        profile_stage_start = export_profile_stage_begin(profile_frame);
        apply_llrawproc_locked(mlv_data, dng_data->image_buf_unpacked,
                               dng_data->image_size_unpacked);
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_LLRAWPROC,
                                 profile_stage_start);
        export_profile_note_llrawproc_breakdown(profile_frame);
        export_profile_note_gpu_export_telemetry(profile_frame);

        if(dng_data->raw_output_state == COMPRESSED_RAW
           || dng_data->raw_output_state == COMPRESSED_ORIG)
        {
            ret = dng_prepare_compressed_frame(
                mlv_data, dng_data, frame_index,
                mlv_data->RAWI.raw_info.bits_per_pixel,
                defer_compress_to_payload, profile_frame);
            if(ret != 0) return ret;
        }
        else
        {
            dng_data->image_size = dng_get_image_size(
                mlv_data, IMG_SIZE_PACKED, frame_index);
            if(dng_data->image_size == 0
               || dng_data->image_size > dng_data->image_capacity)
                return LJ92_ERROR_CORRUPT;
            profile_stage_start = export_profile_stage_begin(profile_frame);
            dng_pack_image_bits(dng_data->image_buf,
                                dng_data->image_buf_unpacked,
                                mlv_data->RAWI.xRes,
                                mlv_data->RAWI.yRes,
                                mlv_data->RAWI.raw_info.bits_per_pixel,
                                1);
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_PACK,
                                     profile_stage_start);
        }
    }
    else
    {
        FILE *fd = mlv_data->file[mlv_data->video_index[frame_index].chunk_num];

        if (isMcrawLoaded(mlv_data))
        {
        /* Move to start of frame in file and read the RAW data */
        profile_stage_start = export_profile_stage_begin(profile_frame);
        if(file_set_pos(fd, mlv_data->video_index[frame_index].block_offset,
                        SEEK_SET) != 0) {
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ,
                                     profile_stage_start);
            return LJ92_ERROR_CORRUPT;
        }

        mr_item_t item = {};

        if (fread(&item, sizeof(mr_item_t), 1, fd) != 1)
        {
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
#ifndef STDOUT_SILENT
            printf("Can not read raw frame from %s\n", mlv_data->path);
#endif
            return -1;
        }

        size_t stored_size = item.size;

        if (stored_size > dng_data->image_capacity) {
            void * replacement = realloc(dng_data->image_buf2, stored_size);
            if(!replacement) {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
                return -1;
            }
            dng_data->image_buf2 = replacement;
        }

        if (fread(dng_data->image_buf2, stored_size, 1, fd) != 1)
        {
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
#ifndef STDOUT_SILENT
            printf("Can not read raw frame from %s\n", mlv_data->path);
#endif
            return -1;
        }
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);

        profile_stage_start = export_profile_stage_begin(profile_frame);
        int64_t ret = mr_decode_video_frame((uint8_t*)dng_data->image_buf_unpacked,
                                            (uint8_t*)dng_data->image_buf2,
                                            stored_size,
                                            mlv_data->RAWI.xRes,
                                            mlv_data->RAWI.yRes,
                                            mlv_data->compression_type);
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_DECODE, profile_stage_start);

        if (ret <= 0)
        {
#ifndef STDOUT_SILENT
            printf("mcraw decoder: Failed with error code (%ld)\n", ret);
#endif
            return -1;
        }

        /* apply low level raw processing to the unpacked_frame */
        profile_stage_start = export_profile_stage_begin(profile_frame);
        apply_llrawproc_locked(mlv_data, dng_data->image_buf_unpacked, dng_data->image_size_unpacked);
        export_profile_stage_end(profile_frame, EXPORT_PROFILE_LLRAWPROC, profile_stage_start);
        export_profile_note_llrawproc_breakdown(profile_frame);
        export_profile_note_gpu_export_telemetry(profile_frame);

        if (dng_data->raw_output_state == COMPRESSED_RAW || dng_data->raw_output_state == COMPRESSED_ORIG)
        {
            ret = dng_prepare_compressed_frame(mlv_data,
                                               dng_data,
                                               frame_index,
                                               mlv_data->RAWI.raw_info.bits_per_pixel,
                                               defer_compress_to_payload,
                                               profile_frame);
        }
        else   // uncompressed and fast pass
        {
            dng_data->image_size = dng_get_image_size(
                mlv_data, IMG_SIZE_PACKED, frame_index);
            if(dng_data->image_size == 0
                || dng_data->image_size > dng_data->image_capacity)
                return LJ92_ERROR_CORRUPT;
            profile_stage_start = export_profile_stage_begin(profile_frame);
            dng_pack_image_bits(dng_data->image_buf,
                                dng_data->image_buf_unpacked,
                                mlv_data->RAWI.xRes,
                                mlv_data->RAWI.yRes,
                                mlv_data->RAWI.raw_info.bits_per_pixel,
                                1);
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_PACK, profile_stage_start);
        }
    }
        else
        {
        /* Move to start of frame in file and read the RAW data */
        profile_stage_start = export_profile_stage_begin(profile_frame);
        if(file_set_pos(fd, mlv_data->video_index[frame_index].frame_offset,
                        SEEK_SET) != 0) {
            export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ,
                                     profile_stage_start);
            return LJ92_ERROR_CORRUPT;
        }

        if (dng_data->raw_input_state == COMPRESSED_RAW) /* If lossless, decompress or pass trough */
        {
            dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_LOSLESS, frame_index);
            if(dng_data->image_size == 0 || dng_data->image_size > dng_data->image_capacity)
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
                return LJ92_ERROR_CORRUPT;
            }
            if(fread(dng_data->image_buf, dng_data->image_size, 1, fd) != 1)
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
#ifndef STDOUT_SILENT
                printf("Can not read raw frame from %s\n", mlv_data->path);
#endif
                return LJ92_ERROR_CORRUPT;
            }
            else
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
            }

            if(dng_data->raw_output_state == COMPRESSED_ORIG)
            {
                // do nothing, compressed raw data is ready to save unchanged
            }
            else
            {
                profile_stage_start = export_profile_stage_begin(profile_frame);
                ret = dng_decompress_image(dng_data->image_buf_unpacked,
                                           dng_data->image_capacity,
                                           dng_data->image_buf,
                                           dng_data->image_size,
                                           mlv_data->RAWI.xRes,
                                           mlv_data->RAWI.yRes,
                                           mlv_data->RAWI.raw_info.bits_per_pixel);
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_DECODE, profile_stage_start);

                /* apply low level raw processing to the unpacked_frame */
                profile_stage_start = export_profile_stage_begin(profile_frame);
                apply_llrawproc_locked(mlv_data, dng_data->image_buf_unpacked, dng_data->image_size_unpacked);
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_LLRAWPROC, profile_stage_start);
                export_profile_note_llrawproc_breakdown(profile_frame);
                export_profile_note_gpu_export_telemetry(profile_frame);

                if(dng_data->raw_output_state == COMPRESSED_RAW)
                {
                    ret = dng_prepare_compressed_frame(
                        mlv_data,
                        dng_data,
                        frame_index,
                        (llrpHQDualIso(mlv_data))
                            ? 16
                            : mlv_data->RAWI.raw_info.bits_per_pixel,
                        defer_compress_to_payload,
                        profile_frame);
                }
                else
                {
                    profile_stage_start = export_profile_stage_begin(profile_frame);
                    if(!llrpHQDualIso(mlv_data))
                    {
                        dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_PACKED, frame_index);
                        dng_pack_image_bits(dng_data->image_buf,
                                            dng_data->image_buf_unpacked,
                                            mlv_data->RAWI.xRes,
                                            mlv_data->RAWI.yRes,
                                            mlv_data->RAWI.raw_info.bits_per_pixel,
                                            1);
                    }
                    else
                    {
                        dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_UNPACKED, frame_index);
                        memcpy(dng_data->image_buf, dng_data->image_buf_unpacked, dng_data->image_size);
                    }
                    export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_PACK, profile_stage_start);
                }
            }
        }
        else /* If uncompressed, unpack to 16bit or pass trough */
        {
            dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_PACKED, frame_index);
            if(dng_data->image_size == 0 || dng_data->image_size > dng_data->image_capacity)
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
                return LJ92_ERROR_CORRUPT;
            }
            if(fread(dng_data->image_buf, dng_data->image_size, 1, fd) != 1)
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
#ifndef STDOUT_SILENT
                printf("Can not read raw frame from %s\n", mlv_data->path);
#endif
                return LJ92_ERROR_CORRUPT;
            }
            else
            {
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_READ, profile_stage_start);
            }

            if(dng_data->raw_output_state == UNCOMPRESSED_ORIG)
            {
                profile_stage_start = export_profile_stage_begin(profile_frame);
                dng_reverse_byte_order(dng_data->image_buf, dng_data->image_size);
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_PACK, profile_stage_start);
            }
            else
            {
                profile_stage_start = export_profile_stage_begin(profile_frame);
                dng_unpack_image_bits(dng_data->image_buf_unpacked,
                                      dng_data->image_buf,
                                      mlv_data->RAWI.xRes,
                                      mlv_data->RAWI.yRes,
                                      mlv_data->RAWI.raw_info.bits_per_pixel);
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_RAW_UNPACK, profile_stage_start);

                /* apply low level raw processing to the unpacked_frame */
                profile_stage_start = export_profile_stage_begin(profile_frame);
                apply_llrawproc_locked(mlv_data, dng_data->image_buf_unpacked, dng_data->image_size_unpacked);
                export_profile_stage_end(profile_frame, EXPORT_PROFILE_LLRAWPROC, profile_stage_start);
                export_profile_note_llrawproc_breakdown(profile_frame);
                export_profile_note_gpu_export_telemetry(profile_frame);

                if(dng_data->raw_output_state == COMPRESSED_RAW)
                {
                    ret = dng_prepare_compressed_frame(
                        mlv_data,
                        dng_data,
                        frame_index,
                        (llrpHQDualIso(mlv_data))
                            ? 16
                            : mlv_data->RAWI.raw_info.bits_per_pixel,
                        defer_compress_to_payload,
                        profile_frame);
                }
                else
                {
                    profile_stage_start = export_profile_stage_begin(profile_frame);
                    if(!llrpHQDualIso(mlv_data))
                    {
                        dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_PACKED, frame_index);
                        dng_pack_image_bits(dng_data->image_buf,
                                            dng_data->image_buf_unpacked,
                                            mlv_data->RAWI.xRes,
                                            mlv_data->RAWI.yRes,
                                            mlv_data->RAWI.raw_info.bits_per_pixel,
                                            1);
                    }
                    else
                    {
                        dng_data->image_size = dng_get_image_size(mlv_data, IMG_SIZE_UNPACKED, frame_index);
                        memcpy(dng_data->image_buf, dng_data->image_buf_unpacked, dng_data->image_size);
                    }
                    export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_PACK, profile_stage_start);
                }
            }
        }
    }
    }

    profile_stage_start = export_profile_stage_begin(profile_frame);
    dng_fill_header(mlv_data, dng_data, frame_index, prop_filename);
    export_profile_stage_end(profile_frame, EXPORT_PROFILE_DNG_HEADER, profile_stage_start);
    return ret;
}

/* init DNG data struct */
dngObject_t * initDngObject(mlvObject_t * mlv_data, int raw_state, double fps, int32_t par[4])
{
    dngObject_t * dng_data = calloc(1, sizeof(dngObject_t));

    if(!dng_data || !mlv_data) {
        free(dng_data);
        return NULL;
    }

    if(mlv_data->RAWI.xRes == 0 || mlv_data->RAWI.yRes == 0
       || mlv_data->RAWI.raw_info.bits_per_pixel == 0
       || mlv_data->RAWI.raw_info.bits_per_pixel > 16) {
        free(dng_data);
        return NULL;
    }

    /* Fast/original export promises byte-for-byte payload pass-through. A
     * CinemaDNG folder may mix compressed and uncompressed frames, while its
     * current source abstraction exposes decoded bayer samples rather than a
     * retained per-frame strip. Refuse this mode instead of silently applying
     * llrawproc and rewriting the payload under a pass-through label. */
    if(isDngFolderLoaded(mlv_data)
       && (raw_state == UNCOMPRESSED_ORIG || raw_state == COMPRESSED_ORIG)) {
        free(dng_data);
        return NULL;
    }

    dng_data->fps_float = fps;
    memcpy(dng_data->par, par, sizeof(int32_t) * 4);

    dng_data->raw_input_state = (mlv_data->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) ? COMPRESSED_RAW : UNCOMPRESSED_RAW;
    dng_data->raw_output_state = (dng_data->raw_input_state && (raw_state == 2)) ? COMPRESSED_ORIG : raw_state;

    dng_data->header_size = HEADER_SIZE;
    dng_data->header_buf = malloc(dng_data->header_size);

    dng_data->image_size_unpacked = dng_get_image_size(mlv_data, IMG_SIZE_UNPACKED, 0);
    size_t compressed_capacity = 0;
    if(dng_data->image_size_unpacked == 0
       || !dng_lj92_output_capacity(mlv_data->RAWI.xRes,
                                 mlv_data->RAWI.yRes,
                                 &compressed_capacity)) {
        free(dng_data->header_buf);
        free(dng_data);
        return NULL;
    }
    dng_data->image_size = dng_data->image_size_unpacked;
    dng_data->image_capacity = compressed_capacity > dng_data->image_size_unpacked
        ? compressed_capacity : dng_data->image_size_unpacked;
    dng_data->image_buf = malloc(dng_data->image_capacity);
    dng_data->image_buf2 = malloc(dng_data->image_capacity);
    dng_data->image_buf_unpacked = malloc(dng_data->image_capacity);

    if(!dng_data->header_buf || !dng_data->image_buf || !dng_data->image_buf2
       || !dng_data->image_buf_unpacked) {
        free(dng_data->header_buf);
        free(dng_data->image_buf);
        free(dng_data->image_buf2);
        free(dng_data->image_buf_unpacked);
        free(dng_data);
        return NULL;
    }

    return dng_data;
}

void setDngExportOverrides(dngObject_t * dng_data, const dngExportOverrides_t * overrides)
{
    if(!dng_data) return;

    memset(&dng_data->overrides, 0, sizeof(dng_data->overrides));
    if(overrides)
    {
        dng_data->overrides = *overrides;
    }
}

static int dng_write_buffers(FILE * dngf,
                             const uint8_t * header_buf,
                             size_t header_size,
                             const uint8_t * image_buf,
                             size_t image_size)
{
    if(!dngf || !header_buf || !image_buf) return 1;

    if(fwrite(header_buf, header_size, 1, dngf) != 1)
    {
        return 1;
    }

    if(fwrite(image_buf, image_size, 1, dngf) != 1)
    {
        return 1;
    }

    return 0;
}

static dngFramePayload_t * dng_take_ready_frame_payload(dngObject_t * dng_data,
                                                        uint32_t frame_index)
{
    if(!dng_data) return NULL;
    if(!dng_data->header_buf || !dng_data->image_buf) return NULL;

    uint16_t * replacement_image = (uint16_t*)malloc(dng_data->image_capacity);
    uint8_t * payload_header = (uint8_t*)malloc(dng_data->header_size);
    if(!replacement_image || !payload_header)
    {
        if(replacement_image) free(replacement_image);
        if(payload_header) free(payload_header);
        return NULL;
    }

    dngFramePayload_t * payload = calloc(1, sizeof(dngFramePayload_t));
    if(!payload)
    {
        free(replacement_image);
        free(payload_header);
        return NULL;
    }

    payload->frame_index = frame_index;
    payload->raw_input_state = dng_data->raw_input_state;
    payload->raw_output_state = dng_data->raw_output_state;
    payload->compression_deferred = dng_data->defer_payload_compress;
    payload->compression_width = dng_data->payload_compress_width;
    payload->compression_height = dng_data->payload_compress_height;
    payload->compression_bpp = dng_data->payload_compress_bpp;
    payload->compression_input_bytes = dng_data->payload_compress_input_bytes;
    payload->header_size = dng_data->header_size;
    payload->image_size = dng_data->image_size;
    payload->image_capacity = dng_data->image_capacity;
    payload->header_buf = payload_header;
    payload->image_buf = (uint8_t*)dng_data->image_buf;

    memcpy(payload->header_buf, dng_data->header_buf, payload->header_size);
    dng_data->image_buf = replacement_image;
    dng_reset_deferred_payload_compress(dng_data);
    return payload;
}

dngFramePayload_t * buildDngFramePayload(mlvObject_t * mlv_data,
                                         dngObject_t * dng_data,
                                         uint32_t frame_index,
                                         const char *prop_filename)
{
    if(!dng_frame_request_is_valid(mlv_data, dng_data, frame_index)) return NULL;

    if(dng_get_frame(mlv_data, dng_data, frame_index, prop_filename, NULL, 0) != 0)
    {
        return NULL;
    }

    return dng_take_ready_frame_payload(dng_data, frame_index);
}

static int dng_patch_header_long_tag(uint8_t * header,
                                     size_t header_size,
                                     uint16_t tag,
                                     uint32_t value)
{
    if(!header || header_size < 10) return 1;

    uint32_t ifd_offset = 0;
    memcpy(&ifd_offset, header + 4, sizeof(ifd_offset));
    if((size_t)ifd_offset + sizeof(uint16_t) > header_size) return 1;

    uint16_t entry_count = 0;
    memcpy(&entry_count, header + ifd_offset, sizeof(entry_count));
    size_t entry_offset = (size_t)ifd_offset + sizeof(uint16_t);
    for(uint16_t i = 0; i < entry_count; i++)
    {
        if(entry_offset + sizeof(struct directory_entry) > header_size)
        {
            return 1;
        }

        struct directory_entry entry;
        memcpy(&entry, header + entry_offset, sizeof(entry));
        if(entry.tag == tag)
        {
            entry.value = value;
            memcpy(header + entry_offset, &entry, sizeof(entry));
            return 0;
        }
        entry_offset += sizeof(struct directory_entry);
    }

    return 1;
}

static int dng_prepare_payload_for_write(dngFramePayload_t * payload,
                                         exportProfileFrame_t * profile_frame)
{
    if(!payload) return 1;
    if(!payload->compression_deferred) return 0;
    if(!payload->image_buf || payload->image_capacity < payload->image_size)
    {
        return 1;
    }
    if(payload->compression_width <= 0 || payload->compression_height <= 0)
    {
        return 1;
    }
    if(payload->compression_bpp == 0)
    {
        return 1;
    }

    size_t output_size = payload->image_capacity;
    double profile_stage_start = export_profile_stage_begin(profile_frame);
    const int ret = dng_compress_image_profiled((uint16_t*)payload->image_buf,
                                                payload->image_capacity,
                                                (uint16_t*)payload->image_buf,
                                                &output_size,
                                                payload->compression_width,
                                                payload->compression_height,
                                                payload->compression_bpp,
                                                profile_frame);
    export_profile_stage_end(profile_frame,
                             EXPORT_PROFILE_DNG_COMPRESS,
                             profile_stage_start);
    export_profile_note_dng_compress_bytes(
        profile_frame,
        payload->compression_input_bytes,
        (uint64_t)output_size,
        ret == 0);
    if(ret != 0)
    {
        return 1;
    }
    if(output_size > UINT32_MAX)
    {
        return 1;
    }

    payload->image_size = output_size;
    if(dng_patch_header_long_tag(payload->header_buf,
                                 payload->header_size,
                                 tcStripByteCounts,
                                 (uint32_t)payload->image_size) != 0)
    {
        return 1;
    }
    return 0;
}

static int writeDngFramePayloadPrepared(dngFramePayload_t * payload,
                                        const char * dng_filename)
{
    if(!payload || !dng_filename) return 1;

    FILE * dngf = fopen(dng_filename, "wb");
    if(!dngf) return 1;

    const int ret = dng_write_buffers(dngf,
                                      payload->header_buf,
                                      payload->header_size,
                                      payload->image_buf,
                                      payload->image_size);
    fclose(dngf);
    return ret;
}

static int writeDngFramePayloadProfiled(dngFramePayload_t * payload,
                                        const char * dng_filename,
                                        exportProfileFrame_t * profile_frame)
{
    if(dng_prepare_payload_for_write(payload, profile_frame) != 0) return 1;
    return writeDngFramePayloadPrepared(payload, dng_filename);
}

int writeDngFramePayload(dngFramePayload_t * payload, const char * dng_filename)
{
    return writeDngFramePayloadProfiled(payload, dng_filename, NULL);
}

void freeDngFramePayload(dngFramePayload_t * payload)
{
    if(!payload) return;
    if(payload->header_buf) free(payload->header_buf);
    if(payload->image_buf) free(payload->image_buf);
    free(payload);
}

static char * dng_strdup(const char * value)
{
    if(!value) return NULL;
    const size_t bytes = strlen(value) + 1;
    char * copy = (char*)malloc(bytes);
    if(copy) memcpy(copy, value, bytes);
    return copy;
}

static void dng_print_saved_frame_status_for_state(int raw_output_state,
                                                   uint32_t frame_index,
                                                   const char * dng_filename)
{
#ifndef STDOUT_SILENT
    if (!frame_index)
    {
        switch (raw_output_state)
        {
            case UNCOMPRESSED_RAW:
                printf("\nWriting uncompressed frames...\n");
                break;
            case COMPRESSED_RAW:
                printf("\nWriting losless frames...\n");
                break;
            case UNCOMPRESSED_ORIG:
                printf("\nPassing through original uncompressed raw...\n");
                break;
            case COMPRESSED_ORIG:
                printf("\nPassing through original lossless raw...\n");
                break;
        }
    }
    printf("Current frame '%s' (frames saved: %u)\n", dng_filename, frame_index + 1);
#else
    (void)raw_output_state;
    (void)frame_index;
    (void)dng_filename;
#endif
}

static void dng_print_saved_frame_status(const dngObject_t * dng_data,
                                         uint32_t frame_index,
                                         const char * dng_filename)
{
    dng_print_saved_frame_status_for_state(
        dng_data ? dng_data->raw_output_state : -1,
        frame_index,
        dng_filename);
}

typedef struct
{
    dngFramePayload_t * payload;
    char * dng_filename;
    exportProfileFrame_t profile_frame;
    double profile_frame_start;
} dngPayloadWriterJob_t;

struct dngPayloadWriter
{
    pthread_t * threads;
    pthread_mutex_t mutex;
    pthread_cond_t can_produce;
    pthread_cond_t can_consume;
    size_t thread_count;
    size_t threads_started;
    int stopping;
    int first_error;
    size_t queue_capacity;
    size_t queue_count;
    size_t queue_head;
    size_t queue_tail;
    size_t active_jobs;
    size_t jobs_started;
    size_t jobs_finished;
    int debug_hold_pending;
    unsigned debug_hold_bound;
    int debug_hold_result;
    dngPayloadWriterJob_t * jobs;
};

static size_t dng_payload_writer_queue_depth_from_env(void)
{
    const char * value = getenv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH");
    char * end = NULL;
    long parsed = 0;
    if(value == NULL || value[0] == '\0')
    {
        return DNG_PAYLOAD_WRITER_DEFAULT_QUEUE_DEPTH;
    }

    parsed = strtol(value, &end, 10);
    if(end == value || parsed < DNG_PAYLOAD_WRITER_DEFAULT_QUEUE_DEPTH)
    {
        return DNG_PAYLOAD_WRITER_DEFAULT_QUEUE_DEPTH;
    }
    if(parsed > DNG_PAYLOAD_WRITER_MAX_QUEUE_DEPTH)
    {
        return DNG_PAYLOAD_WRITER_MAX_QUEUE_DEPTH;
    }
    return (size_t)parsed;
}

static size_t dng_payload_writer_thread_count_from_env(void)
{
    const char * value = getenv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS");
    char * end = NULL;
    long parsed = 0;
    if(value == NULL || value[0] == '\0')
    {
        return DNG_PAYLOAD_WRITER_DEFAULT_THREAD_COUNT;
    }

    parsed = strtol(value, &end, 10);
    if(end == value || parsed < DNG_PAYLOAD_WRITER_DEFAULT_THREAD_COUNT)
    {
        return DNG_PAYLOAD_WRITER_DEFAULT_THREAD_COUNT;
    }
    if(parsed > DNG_PAYLOAD_WRITER_MAX_THREAD_COUNT)
    {
        return DNG_PAYLOAD_WRITER_MAX_THREAD_COUNT;
    }
    return (size_t)parsed;
}

/* Absolute CLOCK_REALTIME deadline bound_ms from now for pthread_cond_timedwait.
 * Returns 0 on success, non-zero when the clock cannot be read. */
static int dng_payload_writer_debug_deadline(unsigned bound_ms, struct timespec * deadline)
{
    if(clock_gettime(CLOCK_REALTIME, deadline) != 0)
    {
        return 1;
    }
    deadline->tv_sec += (time_t)(bound_ms / 1000u);
    deadline->tv_nsec += (long)(bound_ms % 1000u) * 1000000L;
    if(deadline->tv_nsec >= 1000000000L)
    {
        deadline->tv_sec += 1;
        deadline->tv_nsec -= 1000000000L;
    }
    return 0;
}

/* Caller holds writer->mutex. Parks the writer until the queue is full or finish was called.
 * A bounded wait that runs out first, while the producer is still running (stopping unset), is
 * loud: stderr plus first_error, so finish fails. Once finish has set stopping, no further frame
 * can arrive, so a queue that is not full is the producer's choice and the hold ends as STOPPED,
 * not as an error, even when the deadline also passed. The outcome is published in
 * debug_hold_result and can_produce is broadcast so dngPayloadWriterDebugWaitHoldResult wakes. */
static void dng_payload_writer_debug_hold_until_full(dngPayloadWriter_t * writer)
{
    const unsigned bound_ms = writer->debug_hold_bound;
    struct timespec deadline;
    int timed_out = dng_payload_writer_debug_deadline(bound_ms, &deadline);

    while(!timed_out && writer->debug_hold_pending
          && writer->queue_count < writer->queue_capacity && !writer->stopping)
    {
        if(pthread_cond_timedwait(&writer->can_consume, &writer->mutex, &deadline) == ETIMEDOUT)
        {
            timed_out = 1;
        }
    }

    /* Another writer thread already ended the hold and published its outcome. */
    if(!writer->debug_hold_pending)
    {
        return;
    }

    if(writer->queue_count >= writer->queue_capacity)
    {
        writer->debug_hold_result = DNG_PAYLOAD_WRITER_DEBUG_HOLD_FILLED;
    }
    else if(writer->stopping)
    {
        writer->debug_hold_result = DNG_PAYLOAD_WRITER_DEBUG_HOLD_STOPPED;
    }
    else
    {
        fprintf(stderr,
                "[dng] async writer debug hold timed out after %u ms with the queue not full\n",
                bound_ms);
        if(writer->first_error == 0)
        {
            writer->first_error = 1;
        }
        writer->debug_hold_result = DNG_PAYLOAD_WRITER_DEBUG_HOLD_TIMED_OUT;
    }
    writer->debug_hold_pending = 0;
    pthread_cond_broadcast(&writer->can_produce);
}

static void * dng_payload_writer_main(void * opaque)
{
    dngPayloadWriter_t * writer = (dngPayloadWriter_t*)opaque;

    for(;;)
    {
        dngPayloadWriterJob_t job;
        size_t active_jobs = 0;
        size_t jobs_started = 0;
        size_t jobs_finished = 0;
        memset(&job, 0, sizeof(job));

        pthread_mutex_lock(&writer->mutex);
        while(writer->queue_count == 0 && !writer->stopping)
        {
            pthread_cond_wait(&writer->can_consume, &writer->mutex);
        }
        if(writer->queue_count == 0 && writer->stopping)
        {
            pthread_mutex_unlock(&writer->mutex);
            break;
        }
        pthread_mutex_unlock(&writer->mutex);
        dng_payload_writer_debug_delay();
        pthread_mutex_lock(&writer->mutex);
        if(writer->debug_hold_pending)
        {
            dng_payload_writer_debug_hold_until_full(writer);
        }
        if(writer->queue_count == 0)
        {
            pthread_mutex_unlock(&writer->mutex);
            continue;
        }
        job = writer->jobs[writer->queue_head];
        memset(&writer->jobs[writer->queue_head], 0, sizeof(writer->jobs[writer->queue_head]));
        writer->queue_head = (writer->queue_head + 1) % writer->queue_capacity;
        writer->queue_count--;
        writer->active_jobs++;
        writer->jobs_started++;
        active_jobs = writer->active_jobs;
        jobs_started = writer->jobs_started;
        jobs_finished = writer->jobs_finished;
        pthread_cond_signal(&writer->can_produce);
        pthread_mutex_unlock(&writer->mutex);
        export_profile_note_async_writer_activity(active_jobs,
                                                  jobs_started,
                                                  jobs_finished);

        const int prepare_ret =
            dng_prepare_payload_for_write(job.payload, &job.profile_frame);
        int write_ret = 1;
        if(prepare_ret == 0)
        {
            const double write_start = export_profile_stage_begin(&job.profile_frame);
            write_ret = writeDngFramePayloadPrepared(job.payload, job.dng_filename);
            export_profile_stage_end(&job.profile_frame, EXPORT_PROFILE_DISK_WRITE, write_start);
        }
        export_profile_stage_end(&job.profile_frame, EXPORT_PROFILE_FRAME_TOTAL, job.profile_frame_start);
        export_profile_frame_finish(&job.profile_frame, write_ret == 0);

        if(write_ret == 0)
        {
            dng_print_saved_frame_status_for_state(
                job.payload ? job.payload->raw_output_state : -1,
                job.payload ? job.payload->frame_index : 0,
                job.dng_filename ? job.dng_filename : "");
        }

        freeDngFramePayload(job.payload);
        free(job.dng_filename);

        pthread_mutex_lock(&writer->mutex);
        if(write_ret != 0 && writer->first_error == 0)
        {
            writer->first_error = write_ret;
        }
        if(writer->active_jobs > 0)
        {
            writer->active_jobs--;
        }
        writer->jobs_finished++;
        active_jobs = writer->active_jobs;
        jobs_started = writer->jobs_started;
        jobs_finished = writer->jobs_finished;
        pthread_cond_signal(&writer->can_produce);
        pthread_mutex_unlock(&writer->mutex);
        export_profile_note_async_writer_activity(active_jobs,
                                                  jobs_started,
                                                  jobs_finished);
    }

    return NULL;
}

dngPayloadWriter_t * createDngPayloadWriter(void)
{
    dngPayloadWriter_t * writer = (dngPayloadWriter_t*)calloc(1, sizeof(dngPayloadWriter_t));
    if(!writer) return NULL;

    writer->queue_capacity = dng_payload_writer_queue_depth_from_env();
    writer->thread_count = dng_payload_writer_thread_count_from_env();
    writer->debug_hold_bound = dng_payload_writer_debug_hold_bound_ms_from_env();
    writer->debug_hold_pending = writer->debug_hold_bound != 0u;
    writer->debug_hold_result = writer->debug_hold_pending
        ? DNG_PAYLOAD_WRITER_DEBUG_HOLD_PENDING
        : DNG_PAYLOAD_WRITER_DEBUG_HOLD_OFF;
    writer->jobs = (dngPayloadWriterJob_t*)calloc(writer->queue_capacity, sizeof(dngPayloadWriterJob_t));
    if(!writer->jobs)
    {
        free(writer);
        return NULL;
    }
    writer->threads = (pthread_t*)calloc(writer->thread_count, sizeof(pthread_t));
    if(!writer->threads)
    {
        free(writer->jobs);
        free(writer);
        return NULL;
    }

    if(pthread_mutex_init(&writer->mutex, NULL) != 0)
    {
        free(writer->threads);
        free(writer->jobs);
        free(writer);
        return NULL;
    }
    if(pthread_cond_init(&writer->can_produce, NULL) != 0)
    {
        pthread_mutex_destroy(&writer->mutex);
        free(writer->threads);
        free(writer->jobs);
        free(writer);
        return NULL;
    }
    if(pthread_cond_init(&writer->can_consume, NULL) != 0)
    {
        pthread_cond_destroy(&writer->can_produce);
        pthread_mutex_destroy(&writer->mutex);
        free(writer->threads);
        free(writer->jobs);
        free(writer);
        return NULL;
    }
    for(size_t i = 0; i < writer->thread_count; i++)
    {
        if(pthread_create(&writer->threads[i], NULL, dng_payload_writer_main, writer) != 0)
        {
            pthread_mutex_lock(&writer->mutex);
            writer->stopping = 1;
            pthread_cond_broadcast(&writer->can_consume);
            pthread_mutex_unlock(&writer->mutex);
            for(size_t j = 0; j < writer->threads_started; j++)
            {
                pthread_join(writer->threads[j], NULL);
            }
            pthread_cond_destroy(&writer->can_consume);
            pthread_cond_destroy(&writer->can_produce);
            pthread_mutex_destroy(&writer->mutex);
            free(writer->threads);
            free(writer->jobs);
            free(writer);
            return NULL;
        }
        writer->threads_started++;
    }
    export_profile_note_async_writer_threads(writer->thread_count);
    export_profile_note_async_writer_queue(writer->queue_capacity, 0);
    return writer;
}

static int enqueueDngPayloadWriterJob(dngPayloadWriter_t * writer,
                                      dngFramePayload_t * payload,
                                      const char * dng_filename,
                                      exportProfileFrame_t * profile_frame,
                                      double profile_frame_start)
{
    if(!writer || !payload || !dng_filename || !profile_frame) return 1;

    char * filename_copy = dng_strdup(dng_filename);
    if(!filename_copy) return 1;

    double wait_start = 0.0;
    int waited_for_writer = 0;
    size_t queued_after_enqueue = 0;
    size_t queue_capacity = 0;
    pthread_mutex_lock(&writer->mutex);
    while(writer->queue_count >= writer->queue_capacity && writer->first_error == 0 && !writer->stopping)
    {
        if(!waited_for_writer)
        {
            wait_start = export_profile_stage_begin(profile_frame);
            waited_for_writer = 1;
        }
        pthread_cond_wait(&writer->can_produce, &writer->mutex);
    }
    if(waited_for_writer)
    {
        export_profile_stage_end(profile_frame,
                                 EXPORT_PROFILE_WRITER_QUEUE_WAIT,
                                 wait_start);
    }
    if(writer->first_error != 0 || writer->stopping)
    {
        const int ret = writer->first_error ? writer->first_error : 1;
        pthread_mutex_unlock(&writer->mutex);
        free(filename_copy);
        return ret;
    }

    writer->jobs[writer->queue_tail].payload = payload;
    writer->jobs[writer->queue_tail].dng_filename = filename_copy;
    export_profile_stage_end(profile_frame, EXPORT_PROFILE_PRODUCER_FRAME, profile_frame_start);
    writer->jobs[writer->queue_tail].profile_frame = *profile_frame;
    writer->jobs[writer->queue_tail].profile_frame_start = profile_frame_start;
    writer->queue_tail = (writer->queue_tail + 1) % writer->queue_capacity;
    writer->queue_count++;
    queued_after_enqueue = writer->queue_count;
    queue_capacity = writer->queue_capacity;
    pthread_cond_signal(&writer->can_consume);
    if(writer->debug_hold_pending)
    {
        /* Debug hold only: wake every writer thread parked in the hold, not just one. */
        pthread_cond_broadcast(&writer->can_consume);
    }
    pthread_mutex_unlock(&writer->mutex);
    export_profile_note_async_writer_threads(writer->thread_count);
    export_profile_note_async_writer_queue(queue_capacity, queued_after_enqueue);
    export_profile_note_producer_finish(profile_frame);
    return 0;
}

static int dng_should_defer_compress_to_async_writer(const dngObject_t * dng_data)
{
    return dng_data
        && dng_data->raw_output_state == COMPRESSED_RAW
        && dng_async_writer_compress_env_enabled();
}

int dngPayloadWriterDebugWaitHoldResult(dngPayloadWriter_t * writer, unsigned wait_ms)
{
    struct timespec deadline;
    int result = DNG_PAYLOAD_WRITER_DEBUG_HOLD_OFF;
    if(!writer) return DNG_PAYLOAD_WRITER_DEBUG_HOLD_OFF;

    pthread_mutex_lock(&writer->mutex);
    if(writer->debug_hold_result == DNG_PAYLOAD_WRITER_DEBUG_HOLD_PENDING
       && dng_payload_writer_debug_deadline(wait_ms, &deadline) == 0)
    {
        while(writer->debug_hold_result == DNG_PAYLOAD_WRITER_DEBUG_HOLD_PENDING)
        {
            if(pthread_cond_timedwait(&writer->can_produce, &writer->mutex, &deadline) == ETIMEDOUT)
            {
                break;
            }
        }
    }
    result = writer->debug_hold_result;
    pthread_mutex_unlock(&writer->mutex);
    return result;
}

int finishDngPayloadWriter(dngPayloadWriter_t * writer)
{
    if(!writer) return 0;

    pthread_mutex_lock(&writer->mutex);
    writer->stopping = 1;
    pthread_cond_broadcast(&writer->can_consume);
    pthread_mutex_unlock(&writer->mutex);

    for(size_t i = 0; i < writer->threads_started; i++)
    {
        pthread_join(writer->threads[i], NULL);
    }

    const int ret = writer->first_error;
    pthread_cond_destroy(&writer->can_consume);
    pthread_cond_destroy(&writer->can_produce);
    pthread_mutex_destroy(&writer->mutex);
    free(writer->threads);
    free(writer->jobs);
    free(writer);
    return ret;
}

int saveDngFrameViaAsyncPayloadWriter(dngPayloadWriter_t * writer,
                                      mlvObject_t * mlv_data,
                                      dngObject_t * dng_data,
                                      uint32_t frame_index,
                                      char * dng_filename,
                                      const char *prop_filename)
{
    if(!writer || !dng_filename
       || !dng_frame_request_is_valid(mlv_data, dng_data, frame_index)) {
        return 1;
    }

    exportProfileFrame_t profile_frame;
    double profile_frame_start = 0.0;
    double profile_stage_start = 0.0;
    dngFramePayload_t * payload = NULL;

    export_profile_frame_begin(&profile_frame, mlv_data, dng_data, frame_index);
    profile_frame_start = export_profile_stage_begin(&profile_frame);

    if(dng_get_frame(mlv_data,
                     dng_data,
                     frame_index,
                     prop_filename,
                     &profile_frame,
                     dng_should_defer_compress_to_async_writer(dng_data)) != 0)
    {
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    profile_stage_start = export_profile_stage_begin(&profile_frame);
    payload = dng_take_ready_frame_payload(dng_data, frame_index);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_PAYLOAD_CLONE, profile_stage_start);
    if(!payload)
    {
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    const int enqueue_ret = enqueueDngPayloadWriterJob(writer,
                                                       payload,
                                                       dng_filename,
                                                       &profile_frame,
                                                       profile_frame_start);
    if(enqueue_ret != 0)
    {
        freeDngFramePayload(payload);
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    return 0;
}

int saveDngFrameViaPayload(mlvObject_t * mlv_data,
                           dngObject_t * dng_data,
                           uint32_t frame_index,
                           char * dng_filename,
                           const char *prop_filename)
{
    if(!dng_filename
       || !dng_frame_request_is_valid(mlv_data, dng_data, frame_index)) {
        return 1;
    }

    exportProfileFrame_t profile_frame;
    double profile_frame_start = 0.0;
    double profile_stage_start = 0.0;
    dngFramePayload_t * payload = NULL;

    export_profile_frame_begin(&profile_frame, mlv_data, dng_data, frame_index);
    profile_frame_start = export_profile_stage_begin(&profile_frame);

    if(dng_get_frame(mlv_data, dng_data, frame_index, prop_filename, &profile_frame, 0) != 0)
    {
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    profile_stage_start = export_profile_stage_begin(&profile_frame);
    payload = dng_take_ready_frame_payload(dng_data, frame_index);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_PAYLOAD_CLONE, profile_stage_start);
    if(!payload)
    {
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    profile_stage_start = export_profile_stage_begin(&profile_frame);
    const int write_ret = writeDngFramePayload(payload, dng_filename);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_DISK_WRITE, profile_stage_start);
    export_profile_producer_finish(&profile_frame, profile_frame_start);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
    export_profile_frame_finish(&profile_frame, write_ret == 0);
    freeDngFramePayload(payload);

    if(write_ret != 0)
    {
        return 1;
    }

    dng_print_saved_frame_status(dng_data, frame_index, dng_filename);
    return 0;
}

/* save DNG file */
int saveDngFrame(mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, char * dng_filename, const char *prop_filename)
{
    if(!dng_filename
       || !dng_frame_request_is_valid(mlv_data, dng_data, frame_index)) return 1;
    exportProfileFrame_t profile_frame;
    double profile_frame_start = 0.0;
    double profile_stage_start = 0.0;

    export_profile_frame_begin(&profile_frame, mlv_data, dng_data, frame_index);
    profile_frame_start = export_profile_stage_begin(&profile_frame);

    FILE* dngf = fopen(dng_filename, "wb");
    if (!dngf)
    {
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    /* get filled dng_data struct */
    if(dng_get_frame(mlv_data, dng_data, frame_index, prop_filename, &profile_frame, 0) != 0)
    {
        fclose(dngf);
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    profile_stage_start = export_profile_stage_begin(&profile_frame);

    if(dng_write_buffers(dngf,
                         dng_data->header_buf,
                         dng_data->header_size,
                         (const uint8_t *)dng_data->image_buf,
                         dng_data->image_size) != 0)
    {
        fclose(dngf);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_DISK_WRITE, profile_stage_start);
        export_profile_producer_finish(&profile_frame, profile_frame_start);
        export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
        export_profile_frame_finish(&profile_frame, 0);
        return 1;
    }

    fclose(dngf);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_DISK_WRITE, profile_stage_start);
    export_profile_producer_finish(&profile_frame, profile_frame_start);
    export_profile_stage_end(&profile_frame, EXPORT_PROFILE_FRAME_TOTAL, profile_frame_start);
    export_profile_frame_finish(&profile_frame, 1);
    dng_print_saved_frame_status(dng_data, frame_index, dng_filename);
    return 0;
}

/* free all buffers used for DNG creation */
void freeDngObject(dngObject_t * dng_data)
{
    if(!dng_data) return;
    export_profile_write_json();
    if(dng_data->header_buf) free(dng_data->header_buf);
    if(dng_data->image_buf) free(dng_data->image_buf);
    if(dng_data->image_buf2) free(dng_data->image_buf2);
    if(dng_data->image_buf_unpacked) free(dng_data->image_buf_unpacked);
    free(dng_data);
}
