/* Yeas, we have another background thread */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <time.h>
#if defined(_OPENMP)
#include <omp.h>
#endif
#if defined(__WIN32)
#include <windows.h>
#endif

#include "video_mlv.h"
#include "../debayer/debayer.h"
#include "../ca_correct/CA_correct_RT.h"
#include "../debayer/wb_conversion.h"
#include "pipeline_stage_capture.h"

#include "librtprocesswrapper.h"

#define MIN(X, Y) (((X) < (Y)) ? (X) : (Y))
#define MAX(X, Y) (((X) > (Y)) ? (X) : (Y))
#define LIMIT16(X) MAX(MIN(X, 65535), 0)

#if defined(_MSC_VER)
#define MLV_DEBAYER_THREAD_LOCAL __declspec(thread)
#else
#define MLV_DEBAYER_THREAD_LOCAL __thread
#endif

static MLV_DEBAYER_THREAD_LOCAL double g_mlv_last_debayer_wb_prepare_ms = 0.0;
static MLV_DEBAYER_THREAD_LOCAL double g_mlv_last_debayer_ca_ms = 0.0;
static MLV_DEBAYER_THREAD_LOCAL double g_mlv_last_debayer_kernel_ms = 0.0;
static MLV_DEBAYER_THREAD_LOCAL double g_mlv_last_debayer_wb_undo_ms = 0.0;

static double mlv_debayer_timing_now_seconds(void)
{
#if defined(_OPENMP)
    return omp_get_wtime();
#elif defined(__WIN32)
    LARGE_INTEGER frequency;
    LARGE_INTEGER counter;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ((double)ts.tv_nsec / 1000000000.0);
#endif
}

#ifndef STDOUT_SILENT
#define DEBUG(CODE) CODE
#else
#define DEBUG(CODE)
#endif

int mlvCacheWorkerCount(mlvObject_t * video)
{
    if (!video) return 0;
    pthread_mutex_lock(&video->g_mutexCount);
    const int count = video->cache_thread_count;
    pthread_mutex_unlock(&video->g_mutexCount);
    return count;
}

int mlvCacheShouldStop(mlvObject_t * video)
{
    if (!video) return 1;
    pthread_mutex_lock(&video->g_mutexCount);
    const int stop = video->stop_caching;
    pthread_mutex_unlock(&video->g_mutexCount);
    return stop;
}

void mlvCacheSetStop(mlvObject_t * video, int stop)
{
    if (!video) return;
    pthread_mutex_lock(&video->g_mutexCount);
    if (!video->cache_closing || stop)
        video->stop_caching = stop != 0;
    pthread_mutex_unlock(&video->g_mutexCount);
}

/* Claim lifecycle admission before waiting on the serialization mutex.  The
 * claim lets teardown close admission and wait for both the current owner and
 * already-queued callers without destroying a mutex beneath a waiter. */
static int mlv_cache_lifecycle_enter(mlvObject_t * video)
{
    if (!video) return 0;
    pthread_mutex_lock(&video->g_mutexCount);
    if (video->cache_closing)
    {
        pthread_mutex_unlock(&video->g_mutexCount);
        return 0;
    }
    video->cache_lifecycle_users++;
    pthread_mutex_unlock(&video->g_mutexCount);

    pthread_mutex_lock(&video->g_mutexCacheLifecycle);
    pthread_mutex_lock(&video->g_mutexCount);
    const int closing = video->cache_closing;
    pthread_mutex_unlock(&video->g_mutexCount);
    if (closing)
    {
        pthread_mutex_unlock(&video->g_mutexCacheLifecycle);
        pthread_mutex_lock(&video->g_mutexCount);
        video->cache_lifecycle_users--;
        pthread_mutex_unlock(&video->g_mutexCount);
        return 0;
    }
    return 1;
}

static void mlv_cache_lifecycle_leave(mlvObject_t * video)
{
    pthread_mutex_unlock(&video->g_mutexCacheLifecycle);
    pthread_mutex_lock(&video->g_mutexCount);
    video->cache_lifecycle_users--;
    pthread_mutex_unlock(&video->g_mutexCount);
}

void resetMlvLastDebayerStageMilliseconds(void)
{
    g_mlv_last_debayer_wb_prepare_ms = 0.0;
    g_mlv_last_debayer_ca_ms = 0.0;
    g_mlv_last_debayer_kernel_ms = 0.0;
    g_mlv_last_debayer_wb_undo_ms = 0.0;
}

double getMlvLastDebayerWbPrepareMilliseconds(void)
{
    return g_mlv_last_debayer_wb_prepare_ms;
}

double getMlvLastDebayerCaMilliseconds(void)
{
    return g_mlv_last_debayer_ca_ms;
}

double getMlvLastDebayerKernelMilliseconds(void)
{
    return g_mlv_last_debayer_kernel_ms;
}

double getMlvLastDebayerWbUndoMilliseconds(void)
{
    return g_mlv_last_debayer_wb_undo_ms;
}

void invalidateMlvProcessedPreviewCache(mlvObject_t * video)
{
    if (!video)
    {
        return;
    }

    video->current_processed_frame_active = 0;
    video->current_processed_frame = 0;
    video->current_processed_frame_threads = 0;
    video->current_processed_frame_signature = 0;
    video->processed_16bit_cache_next_slot = 0;
    for (uint32_t slot = 0; slot < MLV_PROCESSED_16BIT_CACHE_SLOTS; ++slot)
    {
        video->processed_16bit_cache_active[slot] = 0;
        video->processed_16bit_cache_frame[slot] = 0;
        video->processed_16bit_cache_threads[slot] = 0;
        video->processed_16bit_cache_signature[slot] = 0;
    }
    mlvInvalidateProcessed8PrefetchCache(video);
}

static uint64_t mlv_cache_max_start(mlvObject_t * video)
{
    if (getMlvFrames(video) <= getMlvRawCacheLimitFrames(video)) return 0;
    return getMlvFrames(video) - getMlvRawCacheLimitFrames(video);
}

static uint64_t mlv_cache_clamp_start(mlvObject_t * video, uint64_t startFrame)
{
    return MIN(startFrame, mlv_cache_max_start(video));
}

static uint64_t mlv_cache_window_end(mlvObject_t * video)
{
    return MIN(getMlvFrames(video), mlv_cache_clamp_start(video, video->cache_start_frame) + getMlvRawCacheLimitFrames(video));
}

static uint64_t mlv_cache_window_end_for_start(mlvObject_t * video, uint64_t startFrame)
{
    return MIN(getMlvFrames(video), mlv_cache_clamp_start(video, startFrame) + getMlvRawCacheLimitFrames(video));
}

static uint64_t mlv_cache_desired_start_for_frame(mlvObject_t * video, uint64_t frameIndex)
{
    uint64_t frame_limit = getMlvRawCacheLimitFrames(video);
    uint64_t start = mlv_cache_clamp_start(video, video->cache_start_frame);
    uint64_t end = mlv_cache_window_end(video);

    if (frame_limit == 0 || getMlvFrames(video) <= frame_limit)
    {
        return 0;
    }

    if (frameIndex < start)
    {
        return mlv_cache_clamp_start(video, frameIndex);
    }

    if (frameIndex >= end)
    {
        if (frameIndex + 1 <= frame_limit)
        {
            return 0;
        }
        return mlv_cache_clamp_start(video, frameIndex + 1 - frame_limit);
    }

    return start;
}

static void mlv_cache_restore_linear_slots(mlvObject_t * video)
{
    uint64_t frame_pix = (uint64_t)getMlvWidth(video) * getMlvHeight(video) * 3;
    uint64_t frame_limit = getMlvRawCacheLimitFrames(video);

    if (!video->rgb_raw_frames || !video->cache_memory_block)
    {
        return;
    }

    for (uint64_t i = 0; i < frame_limit; ++i)
    {
        video->rgb_raw_frames[i] = video->cache_memory_block + (frame_pix * i);
    }
}

static int mlv_cache_preserve_overlap_locked(mlvObject_t * video, uint64_t old_start, uint64_t new_start)
{
    uint64_t frame_limit = getMlvRawCacheLimitFrames(video);
    uint64_t old_end = mlv_cache_window_end_for_start(video, old_start);
    uint64_t new_end = mlv_cache_window_end_for_start(video, new_start);
    uint64_t overlap_start = MAX(old_start, new_start);
    uint64_t overlap_end = MIN(old_end, new_end);
    uint16_t ** remapped_slots = NULL;
    uint8_t * consumed_slots = NULL;
    uint64_t next_free_slot = 0;

    if (!video->rgb_raw_frames || !video->cache_memory_block || frame_limit == 0)
    {
        return 0;
    }

    remapped_slots = (uint16_t **)calloc((size_t)frame_limit, sizeof(uint16_t *));
    consumed_slots = (uint8_t *)calloc((size_t)frame_limit, sizeof(uint8_t));
    if (!remapped_slots || !consumed_slots)
    {
        free(remapped_slots);
        free(consumed_slots);
        return 0;
    }

    for (uint64_t frame = overlap_start; frame < overlap_end; ++frame)
    {
        uint64_t old_slot = frame - old_start;
        uint64_t new_slot = frame - new_start;
        remapped_slots[new_slot] = video->rgb_raw_frames[old_slot];
        consumed_slots[old_slot] = 1;
    }

    for (uint64_t slot = 0; slot < frame_limit; ++slot)
    {
        if (remapped_slots[slot]) continue;

        while (next_free_slot < frame_limit && consumed_slots[next_free_slot])
        {
            ++next_free_slot;
        }

        if (next_free_slot >= frame_limit)
        {
            free(remapped_slots);
            free(consumed_slots);
            return 0;
        }

        remapped_slots[slot] = video->rgb_raw_frames[next_free_slot];
        consumed_slots[next_free_slot] = 1;
    }

    memcpy(video->rgb_raw_frames, remapped_slots, (size_t)frame_limit * sizeof(uint16_t *));

    free(remapped_slots);
    free(consumed_slots);
    return 1;
}

int mlv_frame_in_cache_window(mlvObject_t * video, uint64_t frameIndex)
{
    if (!isMlvActive(video) || getMlvRawCacheLimitFrames(video) == 0) return 0;
    uint64_t start = mlv_cache_clamp_start(video, video->cache_start_frame);
    uint64_t end = MIN(getMlvFrames(video), start + getMlvRawCacheLimitFrames(video));
    return frameIndex >= start && frameIndex < end;
}

uint64_t mlv_cache_slot_for_frame(mlvObject_t * video, uint64_t frameIndex)
{
    return frameIndex - mlv_cache_clamp_start(video, video->cache_start_frame);
}

static void mlv_cache_ensure_window_admitted(mlvObject_t * video,
                                             uint64_t frameIndex)
{
    if (!isMlvActive(video) || mlvCacheShouldStop(video) || getMlvRawCacheLimitFrames(video) == 0) return;

    pthread_mutex_lock( &video->g_mutexFind );
    int in_window = mlv_frame_in_cache_window(video, frameIndex);
    uint64_t desired_start = mlv_cache_desired_start_for_frame(video, frameIndex);
    pthread_mutex_unlock( &video->g_mutexFind );
    if (in_window)
    {
        return;
    }

    mlvCacheSetStop(video, 1);
    while (mlvCacheWorkerCount(video)) usleep(100);

    pthread_mutex_lock( &video->g_mutexFind );
    uint64_t old_start = mlv_cache_clamp_start(video, video->cache_start_frame);
    uint64_t old_end = mlv_cache_window_end_for_start(video, old_start);
    int overlap_preserved = mlv_cache_preserve_overlap_locked(video, old_start, desired_start);
    video->cache_start_frame = desired_start;
    video->cache_next = 0;
    video->cache_generation++;

    if (!overlap_preserved)
    {
        mlv_cache_restore_linear_slots(video);
    }

    for (uint64_t i = 0; i < getMlvFrames(video); ++i)
    {
        if (!overlap_preserved
            || !mlv_frame_in_cache_window(video, i)
            || i < old_start
            || i >= old_end)
        {
            video->cached_frames[i] = MLV_FRAME_NOT_CACHED;
        }
    }
    video->current_cached_frame_active = 0;
    pthread_mutex_unlock( &video->g_mutexFind );

    mlvCacheSetStop(video, 0);
    for (int i = 0; i < video->cpu_cores; ++i)
    {
        add_mlv_cache_thread(video);
    }
}

void mlv_cache_ensure_window(mlvObject_t * video, uint64_t frameIndex)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    mlv_cache_ensure_window_admitted(video, frameIndex);
    mlv_cache_lifecycle_leave(video);
}

void mlv_cache_request_playback_preroll(mlvObject_t * video,
                                        uint64_t currentFrame,
                                        uint64_t lastFrameInclusive,
                                        uint64_t lookaheadFrames)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    if (!isMlvActive(video)
        || mlvCacheShouldStop(video)
        || getMlvRawCacheLimitFrames(video) == 0
        || getMlvFrames(video) == 0)
    {
        mlv_cache_lifecycle_leave(video);
        return;
    }

    const uint64_t maxFrame = getMlvFrames(video) - 1;
    if (currentFrame > maxFrame) currentFrame = maxFrame;
    if (lastFrameInclusive > maxFrame) lastFrameInclusive = maxFrame;
    if (lastFrameInclusive < currentFrame) lastFrameInclusive = currentFrame;

    uint64_t targetFrame = currentFrame;
    if (lookaheadFrames > 0 && currentFrame < lastFrameInclusive)
    {
        const uint64_t remaining = lastFrameInclusive - currentFrame;
        targetFrame = currentFrame + MIN(lookaheadFrames, remaining);
    }

    mlv_cache_ensure_window_admitted(video, targetFrame);

    uint64_t requestFrame = 0;
    int haveRequest = 0;

    pthread_mutex_lock(&video->g_mutexFind);
    uint64_t requestStart = currentFrame;
    if (requestStart < lastFrameInclusive)
    {
        requestStart++;
    }

    for (uint64_t frame = requestStart; frame <= targetFrame; ++frame)
    {
        if (mlv_frame_in_cache_window(video, frame)
            && video->cached_frames[frame] == MLV_FRAME_NOT_CACHED)
        {
            requestFrame = frame;
            haveRequest = 1;
            break;
        }
    }

    if (!haveRequest
        && currentFrame > 0
        && currentFrame <= lastFrameInclusive
        && mlv_frame_in_cache_window(video, currentFrame)
        && video->cached_frames[currentFrame] == MLV_FRAME_NOT_CACHED)
    {
        requestFrame = currentFrame;
        haveRequest = 1;
    }

    if (haveRequest)
    {
        video->cache_next = requestFrame;
    }
    pthread_mutex_unlock(&video->g_mutexFind);

    int shouldWakeWorkers = 0;
    if (haveRequest && video->cpu_cores > 0)
    {
        pthread_mutex_lock(&video->g_mutexCount);
        shouldWakeWorkers = (!video->stop_caching && video->cache_thread_count == 0);
        pthread_mutex_unlock(&video->g_mutexCount);
    }

    if (shouldWakeWorkers)
    {
        for (int i = 0; i < video->cpu_cores; ++i)
        {
            add_mlv_cache_thread(video);
        }
    }
    mlv_cache_lifecycle_leave(video);
}

static void mark_mlv_uncached_locked(mlvObject_t * video);

void resetMlvCache(mlvObject_t * video)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    resetMlvCachedFrame(video);
    invalidateMlvProcessedPreviewCache(video);
    pthread_mutex_lock( &video->g_mutexFind );
    mark_mlv_uncached_locked(video);
    pthread_mutex_unlock( &video->g_mutexFind );
    mlv_cache_lifecycle_leave(video);
}

static int mlv_cache_quiesce_workers(mlvObject_t * video);
static void mlv_cache_restart_workers_if_needed(mlvObject_t * video, int restart);
static void mlv_cache_pause_lifecycle_for_testing(void);
static void set_mlv_raw_cache_limit_megabytes_admitted(mlvObject_t * video,
                                                        uint64_t megaByteLimit,
                                                        int force_running);

void disableMlvCaching(mlvObject_t * video)
{
    if (!video) return;
    if (!mlv_cache_lifecycle_enter(video)) return;
    (void)mlv_cache_quiesce_workers(video);
    mlv_cache_pause_lifecycle_for_testing();
    /* Remove the memory (it's a tradition in MLV App libraries to leave a couple of bytes). */
    uint16_t * replacement = (uint16_t *)malloc(2u);
    uint16_t ** replacement_frames = (uint16_t **)malloc(sizeof(uint16_t *));
    pthread_mutex_lock(&video->g_mutexFind);
    mark_mlv_uncached_locked(video);
    if (replacement && replacement_frames)
    {
        uint16_t * old_block = video->cache_memory_block;
        uint16_t ** old_frames = video->rgb_raw_frames;
        replacement_frames[0] = replacement;
        video->cache_memory_block = replacement;
        video->rgb_raw_frames = replacement_frames;
        video->cache_limit_frames = 0;
        pthread_mutex_unlock(&video->g_mutexFind);
        free(old_block);
        free(old_frames);
        mlv_cache_lifecycle_leave(video);
        return;
    }
    pthread_mutex_unlock(&video->g_mutexFind);
    free(replacement);
    free(replacement_frames);
    mlv_cache_lifecycle_leave(video);
}

void enableMlvCaching(mlvObject_t * video)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    set_mlv_raw_cache_limit_megabytes_admitted(video,
                                                video->cache_limit_mb,
                                                1);
    mlv_cache_lifecycle_leave(video);
}

/* Hmmmm, did anyone need 2 ways of doing this? */

static int g_mlv_cache_resize_allocation_failure_for_testing = -1;

void mlvCacheSetResizeAllocationFailureForTesting(int allocation_index)
{
    g_mlv_cache_resize_allocation_failure_for_testing = allocation_index;
}

static int mlv_cache_checked_frame_layout(mlvObject_t * video,
                                          uint64_t * frame_pixels,
                                          uint64_t * frame_size)
{
    if (!video || !frame_pixels || !frame_size)
        return 0;
    const int width = getMlvWidth(video);
    const int height = getMlvHeight(video);
    if (width <= 0 || height <= 0)
        return 0;
    const uint64_t checked_width = (uint64_t)width;
    const uint64_t checked_height = (uint64_t)height;
    if (checked_width > UINT64_MAX / checked_height)
        return 0;
    const uint64_t pixels = checked_width * checked_height;
    if (pixels > UINT64_MAX / 3u)
        return 0;
    const uint64_t rgb_pixels = pixels * 3u;
    if (rgb_pixels > UINT64_MAX / sizeof(uint16_t))
        return 0;
    *frame_pixels = rgb_pixels;
    *frame_size = rgb_pixels * sizeof(uint16_t);
    return 1;
}

static void mark_mlv_uncached_locked(mlvObject_t * video)
{
    video->cache_generation++;
    video->cache_next = 0;
    invalidateMlvProcessedPreviewCache(video);
    for (uint64_t i = 0; i < getMlvFrames(video); ++i)
        video->cached_frames[i] = MLV_FRAME_NOT_CACHED;
}

static int mlv_cache_resize_transactional(mlvObject_t * video,
                                          uint64_t frame_limit,
                                          uint64_t cache_limit_bytes,
                                          uint64_t cache_limit_mb,
                                          uint64_t frame_pixels,
                                          uint64_t frame_size)
{
    if (!video
        || frame_limit > SIZE_MAX / sizeof(uint16_t *)
        || (frame_limit != 0 && frame_size > UINT64_MAX / frame_limit))
    {
        return 0;
    }
    const uint64_t block_bytes_u64 = frame_size * frame_limit;
    if (block_bytes_u64 > SIZE_MAX)
        return 0;
    const size_t block_bytes = block_bytes_u64 ? (size_t)block_bytes_u64 : 2u;
    const size_t pointer_bytes = frame_limit
        ? (size_t)frame_limit * sizeof(uint16_t *)
        : sizeof(uint16_t *);

    uint16_t * replacement_block =
        g_mlv_cache_resize_allocation_failure_for_testing == 0
        ? NULL
        : (uint16_t *)malloc(block_bytes);
    uint16_t ** replacement_frames =
        g_mlv_cache_resize_allocation_failure_for_testing == 1
        ? NULL
        : (uint16_t **)malloc(pointer_bytes);
    if (!replacement_block || !replacement_frames)
    {
        free(replacement_block);
        free(replacement_frames);
        return 0;
    }
    for (uint64_t i = 0; i < frame_limit; ++i)
        replacement_frames[i] = replacement_block + (size_t)(frame_pixels * i);

    pthread_mutex_lock(&video->g_mutexFind);
    mark_mlv_uncached_locked(video);
    uint16_t * old_block = video->cache_memory_block;
    uint16_t ** old_frames = video->rgb_raw_frames;
    video->cache_memory_block = replacement_block;
    video->rgb_raw_frames = replacement_frames;
    video->cache_limit_frames = frame_limit;
    video->cache_limit_bytes = cache_limit_bytes;
    video->cache_limit_mb = cache_limit_mb;
    pthread_mutex_unlock(&video->g_mutexFind);
    free(old_block);
    free(old_frames);
    return 1;
}

static int mlv_cache_quiesce_workers(mlvObject_t * video)
{
    const int restart = !mlvCacheShouldStop(video);
    mlvCacheSetStop(video, 1);
    while (mlvCacheWorkerCount(video)) usleep(100);
    return restart;
}

static void mlv_cache_restart_workers_if_needed(mlvObject_t * video, int restart)
{
    if (!restart) return;
    mlvCacheSetStop(video, 0);
    for (int i = 0; i < video->cpu_cores; ++i)
        add_mlv_cache_thread(video);
}

static void set_mlv_raw_cache_limit_megabytes_admitted(mlvObject_t * video,
                                                        uint64_t megaByteLimit,
                                                        int force_running)
{
    uint64_t frame_pix = 0;
    uint64_t frame_size = 0;
    if (megaByteLimit > UINT64_MAX / (1u << 20)) return;
    const uint64_t bytes_limit = megaByteLimit * (1u << 20);
    if (!isMlvActive(video))
    {
        video->cache_limit_mb = megaByteLimit;
        video->cache_limit_bytes = bytes_limit;
        if (force_running)
        {
            pthread_mutex_lock(&video->g_mutexCount);
            video->stop_caching = 0;
            pthread_mutex_unlock(&video->g_mutexCount);
        }
        return;
    }
    if (!mlv_cache_checked_frame_layout(video, &frame_pix, &frame_size))
        return;
    const uint64_t frame_limit = MIN(bytes_limit / frame_size, getMlvFrames(video));

    const int was_running = mlv_cache_quiesce_workers(video);
    mlv_cache_pause_lifecycle_for_testing();
    const int resized = mlv_cache_resize_transactional(video,
                                                       frame_limit,
                                                       bytes_limit,
                                                       megaByteLimit,
                                                       frame_pix,
                                                       frame_size);
    /* Ordinary sizing preserves the caller's prior running state even if a
     * replacement allocation fails and the old topology is retained.  An
     * explicit enable starts a stopped cache only after materialization, but
     * an idempotent enable keeps an already-running preserved topology live. */
    mlv_cache_restart_workers_if_needed(video,
                                        force_running ? (resized || was_running)
                                                      : was_running);
}

/* What I call MegaBytes is actually MebiBytes! I'm so upset to find that out :( */
void setMlvRawCacheLimitMegaBytes(mlvObject_t * video, uint64_t megaByteLimit)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    set_mlv_raw_cache_limit_megabytes_admitted(video, megaByteLimit, 0);
    mlv_cache_lifecycle_leave(video);
}

/* Not recommended */
void setMlvRawCacheLimitFrames(mlvObject_t * video, uint64_t frameLimit)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    uint64_t frame_pix = 0;
    uint64_t frame_size = 0;
    if (!isMlvActive(video)
        || !mlv_cache_checked_frame_layout(video, &frame_pix, &frame_size))
    {
        mlv_cache_lifecycle_leave(video);
        return;
    }
    const uint64_t frame_limit = MIN(frameLimit, getMlvFrames(video));
    if (frame_limit != 0 && frame_size > UINT64_MAX / frame_limit)
    {
        mlv_cache_lifecycle_leave(video);
        return;
    }
    const uint64_t bytes_limit = frame_size * frame_limit;
    const uint64_t mbyte_limit = bytes_limit / (1u << 20);

    const int has_caching = mlv_cache_quiesce_workers(video);
    mlv_cache_pause_lifecycle_for_testing();
    (void)mlv_cache_resize_transactional(video,
                                         frame_limit,
                                         bytes_limit,
                                         mbyte_limit,
                                         frame_pix,
                                         frame_size);
    mlv_cache_restart_workers_if_needed(video, has_caching);
    mlv_cache_lifecycle_leave(video);
}

/* Marks all frames as not cached */
void mark_mlv_uncached(mlvObject_t * video)
{
    if (!video || !mlv_cache_lifecycle_enter(video)) return;
    pthread_mutex_lock( &video->g_mutexFind );
    mark_mlv_uncached_locked(video);
    pthread_mutex_unlock( &video->g_mutexFind );
    mlv_cache_lifecycle_leave(video);
}

/* Clears cache by freeing then reallocating (RAM usage down until frames written) */
void clear_mlv_cache(mlvObject_t * video)
{
    if (!video) return;
    if (!mlv_cache_lifecycle_enter(video)) return;
    const int restart = mlv_cache_quiesce_workers(video);
    mlv_cache_pause_lifecycle_for_testing();
    uint64_t frame_pixels = 0;
    uint64_t frame_size = 0;
    const uint64_t frame_limit = MIN(video->cache_limit_frames, getMlvFrames(video));
    if (!mlv_cache_checked_frame_layout(video, &frame_pixels, &frame_size)
        || (frame_limit != 0 && frame_size > UINT64_MAX / frame_limit))
    {
        mlv_cache_restart_workers_if_needed(video, restart);
        mlv_cache_lifecycle_leave(video);
        return;
    }
    const uint64_t block_bytes_u64 = frame_size * frame_limit;
    if (block_bytes_u64 > SIZE_MAX)
    {
        mlv_cache_restart_workers_if_needed(video, restart);
        mlv_cache_lifecycle_leave(video);
        return;
    }
    const size_t bytes = block_bytes_u64 ? (size_t)block_bytes_u64 : 2u;
    uint16_t * replacement =
        g_mlv_cache_resize_allocation_failure_for_testing == 0
        ? NULL
        : (uint16_t *)malloc(bytes);
    if (replacement)
    {
        pthread_mutex_lock(&video->g_mutexFind);
        mark_mlv_uncached_locked(video);
        uint16_t * old_block = video->cache_memory_block;
        video->cache_memory_block = replacement;
        for (uint64_t i = 0; i < frame_limit; ++i)
            video->rgb_raw_frames[i] = replacement + (size_t)(frame_pixels * i);
        pthread_mutex_unlock(&video->g_mutexFind);
        free(old_block);
    }
    mlv_cache_restart_workers_if_needed(video, restart);
    mlv_cache_lifecycle_leave(video);
}

/* Returns 1 on success, or 0 if all are cached */
int find_mlv_frame_to_cache(mlvObject_t * video, uint64_t * index) /* Outputs to *index */
{
    pthread_mutex_lock( &video->g_mutexFind );
    /* If a specific frame was requested */
    if (video->cache_next)
    {
        uint64_t requested = video->cache_next;
        video->cache_next = 0;
        if (mlv_frame_in_cache_window(video, requested))
        {
            *index = requested;
            pthread_mutex_unlock( &video->g_mutexFind );
            return 1;
        }
    }

    uint64_t start = mlv_cache_clamp_start(video, video->cache_start_frame);
    uint64_t end = mlv_cache_window_end(video);
    for (uint64_t frame = start; frame < end; ++frame)
    {
        /* Return index if it is not cached */
        if (video->cached_frames[frame] == MLV_FRAME_NOT_CACHED)
        {
            *index = frame;
            pthread_mutex_unlock( &video->g_mutexFind );
            return 1;
        }
    }
    pthread_mutex_unlock( &video->g_mutexFind );
    return 0;
}

static pthread_mutex_t g_mlv_cache_worker_pause_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_mlv_cache_worker_pause_condition = PTHREAD_COND_INITIALIZER;
static int g_mlv_cache_worker_pause_for_testing = 0;
static pthread_mutex_t g_mlv_cache_publish_pause_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_mlv_cache_publish_pause_condition = PTHREAD_COND_INITIALIZER;
static int g_mlv_cache_publish_pause_for_testing = 0;
static int g_mlv_cache_publish_paused_for_testing = 0;
static pthread_mutex_t g_mlv_cache_lifecycle_pause_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_mlv_cache_lifecycle_pause_condition = PTHREAD_COND_INITIALIZER;
static int g_mlv_cache_lifecycle_pause_for_testing = 0;
static int g_mlv_cache_lifecycle_paused_for_testing = 0;

void mlvCacheSetWorkerStartPauseForTesting(int enabled)
{
    pthread_mutex_lock(&g_mlv_cache_worker_pause_mutex);
    g_mlv_cache_worker_pause_for_testing = enabled != 0;
    if (!g_mlv_cache_worker_pause_for_testing)
        pthread_cond_broadcast(&g_mlv_cache_worker_pause_condition);
    pthread_mutex_unlock(&g_mlv_cache_worker_pause_mutex);
}

void mlvCacheSetWorkerBeforePublishPauseForTesting(int enabled)
{
    pthread_mutex_lock(&g_mlv_cache_publish_pause_mutex);
    g_mlv_cache_publish_pause_for_testing = enabled != 0;
    if (!g_mlv_cache_publish_pause_for_testing)
        pthread_cond_broadcast(&g_mlv_cache_publish_pause_condition);
    pthread_mutex_unlock(&g_mlv_cache_publish_pause_mutex);
}

int mlvCacheWorkerBeforePublishPausedForTesting(void)
{
    pthread_mutex_lock(&g_mlv_cache_publish_pause_mutex);
    const int paused = g_mlv_cache_publish_paused_for_testing;
    pthread_mutex_unlock(&g_mlv_cache_publish_pause_mutex);
    return paused;
}

void mlvCacheSetLifecyclePauseForTesting(int enabled)
{
    pthread_mutex_lock(&g_mlv_cache_lifecycle_pause_mutex);
    g_mlv_cache_lifecycle_pause_for_testing = enabled != 0;
    if (!g_mlv_cache_lifecycle_pause_for_testing)
        pthread_cond_broadcast(&g_mlv_cache_lifecycle_pause_condition);
    pthread_mutex_unlock(&g_mlv_cache_lifecycle_pause_mutex);
}

int mlvCacheLifecyclePausedForTesting(void)
{
    pthread_mutex_lock(&g_mlv_cache_lifecycle_pause_mutex);
    const int paused = g_mlv_cache_lifecycle_paused_for_testing;
    pthread_mutex_unlock(&g_mlv_cache_lifecycle_pause_mutex);
    return paused;
}

static void mlv_cache_pause_lifecycle_for_testing(void)
{
    pthread_mutex_lock(&g_mlv_cache_lifecycle_pause_mutex);
    if (g_mlv_cache_lifecycle_pause_for_testing)
    {
        g_mlv_cache_lifecycle_paused_for_testing = 1;
        pthread_cond_broadcast(&g_mlv_cache_lifecycle_pause_condition);
        while (g_mlv_cache_lifecycle_pause_for_testing)
            pthread_cond_wait(&g_mlv_cache_lifecycle_pause_condition,
                              &g_mlv_cache_lifecycle_pause_mutex);
        g_mlv_cache_lifecycle_paused_for_testing = 0;
    }
    pthread_mutex_unlock(&g_mlv_cache_lifecycle_pause_mutex);
}

static void mlv_cache_pause_before_publish_for_testing(void)
{
    pthread_mutex_lock(&g_mlv_cache_publish_pause_mutex);
    if (g_mlv_cache_publish_pause_for_testing)
    {
        g_mlv_cache_publish_paused_for_testing = 1;
        pthread_cond_broadcast(&g_mlv_cache_publish_pause_condition);
        while (g_mlv_cache_publish_pause_for_testing)
            pthread_cond_wait(&g_mlv_cache_publish_pause_condition,
                              &g_mlv_cache_publish_pause_mutex);
        g_mlv_cache_publish_paused_for_testing = 0;
    }
    pthread_mutex_unlock(&g_mlv_cache_publish_pause_mutex);
}

static void run_mlv_cache_thread(mlvObject_t * video, int counter_already_claimed);
static int get_mlv_raw_frame_debayered_checked(mlvObject_t * video,
                                               uint64_t frame_index,
                                               float * temp_memory,
                                               uint16_t * output_frame,
                                               int debayer_type,
                                               int isolated_analysis);

static void mlv_cache_worker_count_release(mlvObject_t * video)
{
    pthread_mutex_lock(&video->g_mutexCount);
    video->cache_thread_count--;
    pthread_mutex_unlock(&video->g_mutexCount);
}

static void * mlv_cache_thread_entry(void * opaque)
{
    mlvObject_t * video = (mlvObject_t *)opaque;
    pthread_mutex_lock(&g_mlv_cache_worker_pause_mutex);
    while (g_mlv_cache_worker_pause_for_testing)
        pthread_cond_wait(&g_mlv_cache_worker_pause_condition,
                          &g_mlv_cache_worker_pause_mutex);
    pthread_mutex_unlock(&g_mlv_cache_worker_pause_mutex);
    run_mlv_cache_thread(video, 1);
    return NULL;
}

/* Adds one detached thread.  Claim the active count before pthread_create so
 * object destruction cannot observe zero while a worker is pending start. */
void add_mlv_cache_thread(mlvObject_t * video)
{
    if (!video) return;
    pthread_mutex_lock(&video->g_mutexCount);
    if (video->cache_closing || video->stop_caching || !isMlvActive(video))
    {
        pthread_mutex_unlock(&video->g_mutexCount);
        return;
    }
    video->cache_thread_count++;
    pthread_mutex_unlock(&video->g_mutexCount);

    pthread_t thread;
    if (pthread_create(&thread, NULL, mlv_cache_thread_entry, video) != 0)
    {
        mlv_cache_worker_count_release(video);
        return;
    }
    if (pthread_detach(thread) != 0)
    {
        /* A valid newly created thread should detach.  If the platform
         * refuses, join it here so its handle and object lifetime stay owned. */
        (void)pthread_join(thread, NULL);
    }
}

/* Synchronous entry retained for callers/tests; it owns its count claim. */
void an_mlv_cache_thread(mlvObject_t * video)
{
    run_mlv_cache_thread(video, 0);
}

/* Add as many of these as you want :) */
static int g_mlv_cache_allocation_failure_for_testing = 0;
static int g_mlv_cache_raw_acquisition_failure_for_testing = 0;

void mlvCacheSetAllocationFailureForTesting(int enabled)
{
    g_mlv_cache_allocation_failure_for_testing = enabled != 0;
}

void mlvCacheSetRawAcquisitionFailureForTesting(int enabled)
{
    g_mlv_cache_raw_acquisition_failure_for_testing = enabled != 0;
}

static void run_mlv_cache_thread(mlvObject_t * video, int counter_already_claimed)
{
    if (!video) return;
    if (!counter_already_claimed)
    {
        pthread_mutex_lock(&video->g_mutexCount);
        if (video->cache_closing || !isMlvActive(video))
        {
            pthread_mutex_unlock(&video->g_mutexCount);
            return;
        }
        video->cache_thread_count++;
        pthread_mutex_unlock(&video->g_mutexCount);
    }
    if (!isMlvActive(video))
    {
        mlv_cache_worker_count_release(video);
        return;
    }

    uint32_t height = getMlvHeight(video);
    uint32_t width = getMlvWidth(video);
    const size_t checked_width = (size_t)width;
    const size_t checked_height = (size_t)height;
    if (width < 33 || height < 33
        || checked_width > SIZE_MAX / checked_height)
    {
        mlv_cache_worker_count_release(video);
        return;
    }
    const size_t pixelsize = checked_width * checked_height;
    if (pixelsize < 10u
        || pixelsize > SIZE_MAX / 3u
        || pixelsize > SIZE_MAX / (3u * sizeof(uint16_t))
        || pixelsize > SIZE_MAX / sizeof(float)
        || checked_height > SIZE_MAX / sizeof(float *))
    {
        mlv_cache_worker_count_release(video);
        return;
    }

    /* 2d array uglyness */
    const int force_allocation_failure = g_mlv_cache_allocation_failure_for_testing;
    float * __restrict imagefloat1d = force_allocation_failure
        ? NULL
        : (float *)malloc(pixelsize * sizeof(float));
    const size_t publish_words = pixelsize * 3u;
    uint16_t * __restrict publish_frame = force_allocation_failure
        ? NULL
        : (uint16_t *)malloc(publish_words * sizeof(uint16_t));
    if (!imagefloat1d || !publish_frame)
    {
        free(imagefloat1d);
        free(publish_frame);
        mlv_cache_worker_count_release(video);
        return;
    }

    while (1 < 2)
    {
        if (mlvCacheShouldStop(video)) break;

        uint64_t cache_frame;
        uint32_t cache_generation;

        /* If cache finder reurns false, it's time t stop caching */
        if (!find_mlv_frame_to_cache(video, &cache_frame)) break;

        pthread_mutex_lock( &video->g_mutexFind );
        if (!mlv_frame_in_cache_window(video, cache_frame)
            || video->cached_frames[cache_frame] != MLV_FRAME_NOT_CACHED)
        {
            pthread_mutex_unlock( &video->g_mutexFind );
            continue;
        }
        cache_generation = video->cache_generation;
        video->cached_frames[cache_frame] = MLV_FRAME_BEING_CACHED;
        pthread_mutex_unlock( &video->g_mutexFind );

        /* Use the same checked raw-processing, WB, CA and debayer path as the
         * foreground reader.  The historical cache-local approximation
         * skipped WB/CA and left a different RGB result in shared storage. */
        const int demosaic_succeeded =
            get_mlv_raw_frame_debayered_checked(video,
                                                cache_frame,
                                                imagefloat1d,
                                                publish_frame,
                                                doesMlvAlwaysUseAmaze(video),
                                                0);

        if (!demosaic_succeeded)
        {
            pthread_mutex_lock(&video->g_mutexFind);
            if (cache_generation == video->cache_generation
                && video->cached_frames[cache_frame] == MLV_FRAME_BEING_CACHED)
            {
                video->cached_frames[cache_frame] = MLV_FRAME_NOT_CACHED;
            }
            pthread_mutex_unlock(&video->g_mutexFind);
            /* A persistent worker/allocation failure would otherwise select
             * the same NOT_CACHED frame immediately and hot-spin forever.
             * Leave the cache thread; a later explicitly started worker may
             * retry after resources recover. */
            break;
        }

        /* The complete foreground-equivalent frame is worker-private.  A
         * generation change may make the former shared slot reusable, so no
         * shared byte is touched until the final check under g_mutexFind. */
        mlv_cache_pause_before_publish_for_testing();

        pthread_mutex_lock( &video->g_mutexFind );
        const int stale_generation = cache_generation != video->cache_generation;
        if (!stale_generation
            && mlv_frame_in_cache_window(video, cache_frame)
            && video->cached_frames[cache_frame] == MLV_FRAME_BEING_CACHED)
        {
            const uint64_t cache_slot = mlv_cache_slot_for_frame(video, cache_frame);
            memcpy(video->rgb_raw_frames[cache_slot],
                   publish_frame,
                   publish_words * sizeof(uint16_t));
            video->cached_frames[cache_frame] = MLV_FRAME_IS_CACHED;
        }
        else if (!stale_generation
                 && video->cached_frames[cache_frame] == MLV_FRAME_BEING_CACHED)
        {
            video->cached_frames[cache_frame] = MLV_FRAME_NOT_CACHED;
        }
        pthread_mutex_unlock( &video->g_mutexFind );

        DEBUG( printf("Debayered frame %llu/%llu has been cached.\n", cache_frame+1, video->cache_limit_frames); )
    }

    free(imagefloat1d);
    free(publish_frame);

    mlv_cache_worker_count_release(video);
}

/* Post-acquisition tail of the debayered-16 render. On entry the first
 * Width * Height uint16 words of temp_memory hold the llrawproc'd Bayer (the
 * bit_shift widening not yet applied); temp_memory must be Width * Height *
 * sizeof(float). Writes Width * Height * 3 uint16 to output_frame, which must
 * not overlap temp_memory. Shared by get_mlv_raw_frame_debayered_checked and
 * getMlvRawFrameDebayeredFromReconnedRaw16 so the two cannot drift; it reads and
 * writes no frame cache. */
int get_mlv_raw_frame_debayered_from_processed_raw_u16( mlvObject_t * video,
                                                        uint64_t frame_index,
                                                        float * temp_memory,
                                                        uint16_t * output_frame,
                                                        int debayer_type,
                                                        int bit_shift,
                                                        int debayer_threads,
                                                        const char * capture_path_label )
{
    int width = getMlvWidth(video);
    int height = getMlvHeight(video);
    const size_t frame_pixels = (size_t)width * (size_t)height;

    if( debayer_type == 0 )
    {
        uint16_t * raw_frame_u16 = (uint16_t *)temp_memory;
        const double debayer_kernel_start = mlv_debayer_timing_now_seconds();
        debayerBasicU16(output_frame,
                        raw_frame_u16,
                        width,
                        height,
                        debayer_threads,
                        bit_shift);
        g_mlv_last_debayer_kernel_ms = (mlv_debayer_timing_now_seconds() - debayer_kernel_start) * 1000.0;
        return 1;
    }

    if( debayer_type == 2 )
    {
        uint16_t * raw_frame_u16 = (uint16_t *)temp_memory;
        const double debayer_kernel_start = mlv_debayer_timing_now_seconds();
        debayerNoneU16(output_frame,
                       raw_frame_u16,
                       width,
                       height,
                       debayer_threads,
                       bit_shift);
        g_mlv_last_debayer_kernel_ms = (mlv_debayer_timing_now_seconds() - debayer_kernel_start) * 1000.0;
        return 1;
    }

    /* uint16 -> float in place, from the end so no word is overwritten before it is read. */
    {
        const uint16_t * raw_frame_u16 = (const uint16_t *)temp_memory;
        for (int64_t i = (int64_t)frame_pixels - 1; i >= 0; --i)
        {
            temp_memory[i] = (float)((uint32_t)raw_frame_u16[i] << bit_shift);
        }
    }

    wb_convert_info_t wb_info;

    /* WB conversion for ideal debayer result, not for bilinear, easy and non debayer */
    if( !( debayer_type == 0 || debayer_type == 2 || debayer_type == 3 ) )
    {
        const double wb_prepare_start = mlv_debayer_timing_now_seconds();
        wb_convert(&wb_info, temp_memory, width, height, getMlvBlackLevel(video));
        g_mlv_last_debayer_wb_prepare_ms = (mlv_debayer_timing_now_seconds() - wb_prepare_start) * 1000.0;

        /* CA correction, multithreaded, not for bilinear, easy and non debayer because not visible and slow */
        if( video->ca_red <= -0.1 || video->ca_red >= 0.1
         || video->ca_blue <= -0.1 || video->ca_blue >= 0.1 )
        {
            const double ca_start = mlv_debayer_timing_now_seconds();
            /* 2d array for CA correction */
            float ** __restrict imagefloat2d = (float **)malloc(height * sizeof(float *));
            if (!imagefloat2d)
            {
                memset(output_frame, 0, frame_pixels * 3u * sizeof(uint16_t));
                return 0;
            }
            for (int y = 0; y < height; ++y) imagefloat2d[y] = (float *)(temp_memory+(y*width));

            /* the magic CA correction function */
            /*CA_correct_RT(imagefloat2d, 0, 0, width, height,
                          0, 0, width, height,
                          0, video->ca_red, video->ca_blue);*/ /*auto, red, blue*/

            lrtpCaCorrect( imagefloat2d, 0, 0, width, height,
                           0, 0, video->ca_red, video->ca_blue, 0 );
            free(imagefloat2d);
            g_mlv_last_debayer_ca_ms = (mlv_debayer_timing_now_seconds() - ca_start) * 1000.0;
        }
    }

    /* Debayer */
    const double debayer_kernel_start = mlv_debayer_timing_now_seconds();
    if (/*debayer_type == 1 ||*/ debayer_type == 4 || debayer_type == 5 || /*debayer_type == 6 ||*/ debayer_type == 7 || debayer_type == 8)
    {
        //AMaZE and AHD disabled from librtprocess because of bad artifacts
        debayerLibRtProcess(output_frame, temp_memory, width, height, debayer_type, video->processing->cam_matrix);
    }
    else if (debayer_type == 1 )
    {
        if (!debayerAmaze(output_frame,
                          temp_memory,
                          width,
                          height,
                          debayer_threads,
                          getMlvBlackLevel(video)))
        {
            memset(output_frame, 0, frame_pixels * 3u * sizeof(uint16_t));
            return 0;
        }
    }
    else if(debayer_type == 2 || debayer_type == 3)
    {
        /* threaded easy types */
        debayerEasy(output_frame, temp_memory, width, height, debayer_threads, debayer_type);
    }
    else if (debayer_type == 6 )
    {
        debayerAhd(output_frame, temp_memory, width, height);
    }
    else
    {
        /* Debayer quickly (bilinearly) */
        debayerBasic(output_frame, temp_memory, width, height, 1);
    }
    g_mlv_last_debayer_kernel_ms = (mlv_debayer_timing_now_seconds() - debayer_kernel_start) * 1000.0;

    /* WB conversion undo for ideal debayer result */
    if( !( debayer_type == 0 || debayer_type == 2 || debayer_type == 3 ) )
    {
        const double wb_undo_start = mlv_debayer_timing_now_seconds();
        wb_undo(&wb_info, output_frame, width, height, getMlvBlackLevel(video));
        g_mlv_last_debayer_wb_undo_ms = (mlv_debayer_timing_now_seconds() - wb_undo_start) * 1000.0;
    }

    /* S3_debayer capture: post Bayer->RGB16, all debayer algorithms (AMaZE,
     * Bilinear, AHD, LibRtProcess, Easy) converge here before return. Inert
     * when MLVAPP_PIPELINE_CAPTURE_DIR is unset. */
    if( mlv_pipeline_capture_should_capture_frame(frame_index) )
    {
        const char * debayer_label =
            debayer_type == 0 ? "none"   :
            debayer_type == 1 ? "amaze"  :
            debayer_type == 2 ? "easy"   :
            debayer_type == 3 ? "bilinear" :
            debayer_type == 4 ? "ahd"    :
            debayer_type == 5 ? "rcd"    :
            debayer_type == 6 ? "dcb"    :
            debayer_type == 7 ? "lmmse"  :
            debayer_type == 8 ? "igv"    :
            "unknown";
        mlv_pipeline_capture_meta_t meta;
        memset(&meta, 0, sizeof meta);
        meta.stage = MLV_PIPELINE_STAGE_S3_DEBAYER;
        meta.format = MLV_PIPELINE_FORMAT_UINT16_RGB;
        meta.format_label = "uint16_rgb_post_debayer";
        meta.width = width;
        meta.height = height;
        meta.bytes_per_line = width * 3 * (int)sizeof(uint16_t);
        meta.bytes_per_pixel = 3 * (int)sizeof(uint16_t);
        meta.channels = 3;
        meta.bit_depth = 16;
        meta.dual_iso_mode = NULL;
        meta.debayer_mode = debayer_label;
        meta.scaler = "none";
        meta.path_label = capture_path_label;
        mlv_pipeline_capture(frame_index, output_frame, &meta);
    }
    return 1;
}

static int get_mlv_raw_frame_debayered_checked( mlvObject_t * video,
                                                uint64_t frame_index,
                                                float * temp_memory,
                                                uint16_t * output_frame,
                                                int debayer_type,
                                                int isolated_analysis ) /* 0=bilinear 1=amaze ... */
{
    int width = getMlvWidth(video);
    int height = getMlvHeight(video);
    const size_t frame_pixels = (size_t)width * (size_t)height;
    uint16_t * raw_frame_u16 = (uint16_t *)temp_memory;
    int bit_shift = 0;

    /* Get the raw data in B&W and preserve acquisition failure as status.
     * The public void wrapper historically zero-fills on failure, which is
     * not sufficient for a cache publisher: AMaZE can successfully debayer
     * zeros and make a failed decode look cacheable. */
    const int raw_failed = g_mlv_cache_raw_acquisition_failure_for_testing
        || (isolated_analysis
            ? getMlvRawFrameProcessedUint16Direct(video, frame_index, raw_frame_u16, &bit_shift)
            : getMlvRawFrameProcessedUint16(video, frame_index, raw_frame_u16, &bit_shift));
    if (raw_failed)
    {
        if (debayer_type != 0 && debayer_type != 2)
        {
            memset(temp_memory, 0, frame_pixels * sizeof(float));
        }
        memset(output_frame, 0, frame_pixels * 3u * sizeof(uint16_t));
        return 0;
    }

    return get_mlv_raw_frame_debayered_from_processed_raw_u16(
        video,
        frame_index,
        temp_memory,
        output_frame,
        debayer_type,
        bit_shift,
        isolated_analysis ? 1 : getMlvCpuCores(video),
        isolated_analysis
            ? "get_mlv_raw_frame_debayered_isolated_analysis"
            : "get_mlv_raw_frame_debayered");
}

int mlvRawDebayerCacheMayServeFrame( mlvObject_t * video, uint64_t frame_index )
{
    if (!video) return 1;
    if (mlvCacheWorkerCount(video) > 0) return 1;
    const int cache_running = !mlvCacheShouldStop(video);
    pthread_mutex_lock(&video->g_mutexFind);
    const int cache_sized = video->cache_limit_frames > 0;
    const int frame_cached = frame_index < getMlvFrames(video)
        && video->cached_frames
        && video->cached_frames[frame_index] == MLV_FRAME_IS_CACHED;
    pthread_mutex_unlock(&video->g_mutexFind);
    return (cache_running && cache_sized) || frame_cached;
}

void get_mlv_raw_frame_debayered( mlvObject_t * video,
                                  uint64_t frame_index,
                                  float * temp_memory,
                                  uint16_t * output_frame,
                                  int debayer_type ) /* 0=bilinear 1=amaze ... */
{
    (void)get_mlv_raw_frame_debayered_checked(video,
                                              frame_index,
                                              temp_memory,
                                              output_frame,
                                              debayer_type,
                                              0);
}

void get_mlv_raw_frame_debayered_isolated_analysis( mlvObject_t * video,
                                                    uint64_t frame_index,
                                                    float * temp_memory,
                                                    uint16_t * output_frame,
                                                    int debayer_type ) /* 0=bilinear 1=amaze ... */
{
    (void)get_mlv_raw_frame_debayered_checked(video,
                                              frame_index,
                                              temp_memory,
                                              output_frame,
                                              debayer_type,
                                              1);
}
