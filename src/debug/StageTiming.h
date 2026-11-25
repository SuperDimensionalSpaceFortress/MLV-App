#ifndef MLVAPP_STAGE_TIMING_H
#define MLVAPP_STAGE_TIMING_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <omp.h>

/* Platform clock headers for mlv_stage_timing_now(); see the note on that
 * function for why omp_get_wtime() alone is not sufficient. */
#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#    define MLV_STAGE_TIMING_DEFINED_LEAN
#  endif
#  ifndef NOMINMAX
#    define NOMINMAX
#    define MLV_STAGE_TIMING_DEFINED_NOMINMAX
#  endif
#  include <windows.h>
#  ifdef MLV_STAGE_TIMING_DEFINED_LEAN
#    undef WIN32_LEAN_AND_MEAN
#    undef MLV_STAGE_TIMING_DEFINED_LEAN
#  endif
#  ifdef MLV_STAGE_TIMING_DEFINED_NOMINMAX
#    undef NOMINMAX
#    undef MLV_STAGE_TIMING_DEFINED_NOMINMAX
#  endif
#else
#  include <time.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_MSC_VER)
#define MLV_STAGE_THREAD_LOCAL __declspec(thread)
#else
#define MLV_STAGE_THREAD_LOCAL __thread
#endif

enum { MLV_STAGE_TIMING_SNAPSHOT_CAPACITY = 32 };

#define MLV_STAGE_DECODE   "decode"
#define MLV_STAGE_RECON    "recon"
#define MLV_STAGE_PROCESS  "process"
#define MLV_STAGE_DISPLAY  "display"
/* EXPORT-RENDER-PIPE-TIMING-1: the rendered (ProRes) batch export's stages. llrawproc_dual_iso is the CDNG export
 * profile's llrawproc_dual_iso_ms (dng.c) by the same name, noted after every llrawproc note in video_mlv.c;
 * export_scale (scaled frame plans only) and export_pipe_write are per frame, export_pipe_drain and export_verify
 * once per clip on its last frame (src/batch/BatchRunner.cpp). Every caller notes them only while
 * mlv_stage_timing_enabled(): the note functions below write the snapshot before they check the flag. */
#define MLV_STAGE_LLRAWPROC_DUAL_ISO  "llrawproc_dual_iso"
#define MLV_STAGE_EXPORT_SCALE        "export_scale"
#define MLV_STAGE_EXPORT_PIPE_WRITE   "export_pipe_write"
#define MLV_STAGE_EXPORT_PIPE_DRAIN   "export_pipe_drain"
#define MLV_STAGE_EXPORT_VERIFY       "export_verify"

typedef struct
{
    char stage[48];
    double elapsed_ms;
    int used;
} mlv_stage_timing_snapshot_entry_t;

typedef struct
{
    uint64_t frame_index;
    int has_frame_index;
    mlv_stage_timing_snapshot_entry_t entries[MLV_STAGE_TIMING_SNAPSHOT_CAPACITY];
} mlv_stage_timing_snapshot_t;

static MLV_STAGE_THREAD_LOCAL mlv_stage_timing_snapshot_t g_mlv_stage_timing_snapshot = {};

static inline int mlv_stage_timing_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if (!initialized)
    {
        const char * env = getenv("MLVAPP_STAGE_TIMING");
        enabled = (env != NULL && env[0] != '\0' && strcmp(env, "0") != 0);
        initialized = 1;
    }
    return enabled;
}

static inline FILE * mlv_stage_timing_stream(void)
{
    static int initialized = 0;
    static FILE * stream = NULL;

    if (!initialized)
    {
        const char * path = getenv("MLVAPP_STAGE_TIMING_FILE");
        if (path && path[0] != '\0')
        {
            stream = fopen(path, "a");
        }
        initialized = 1;
    }

    return stream ? stream : stderr;
}

/* HIGH-RESOLUTION MONOTONIC CLOCK.
 *
 * This used to be a bare `omp_get_wtime()`. On the MinGW toolchain this project
 * builds with, `omp_get_wtick()` reports EXACTLY 1.000 ms, so every span shorter
 * than a millisecond measured through it printed `ms=0.000`. Measured 2026-09-02
 * across three Bachelor artifact sets and two build SHAs: 27,081 stage-timing
 * values, ZERO of them non-integer. The emit path is `%.3f` with no rounding, so
 * those integers were the CLOCK, not the code being fast.
 *
 * The consequence was not a rounding nuisance. A stage that truly costs 0.4 ms
 * has a MEDIAN of 0 under 1 ms quantization while its MEAN stays near 0.4, and
 * this board attributed frame time by summing MEDIANS -- so real cost was
 * silently reclassified as "unattributed". Sum of medians 2.000 ms versus sum of
 * means 8.816 ms on the same leg: a 4.4x gap that was an artifact of the clock.
 *
 * QueryPerformanceCounter measures 0.0001 ms steps on the same box (10,000x
 * finer) and reads a deliberately sub-millisecond span as 0.2856 ms. The
 * contract is unchanged -- monotonic seconds from an arbitrary epoch, exactly as
 * omp_get_wtime() -- and omp_get_wtime() remains the fallback on every path that
 * can fail, so a platform without QPC keeps the old behaviour rather than none. */
static inline double mlv_stage_timing_now(void)
{
#if defined(_WIN32)
    static LARGE_INTEGER mlv_stage_timing_qpc_freq;
    static int mlv_stage_timing_qpc_ready = 0;
    LARGE_INTEGER counter;

    if (!mlv_stage_timing_qpc_ready)
    {
        if (!QueryPerformanceFrequency(&mlv_stage_timing_qpc_freq)
         || mlv_stage_timing_qpc_freq.QuadPart <= 0)
        {
            return omp_get_wtime();
        }
        mlv_stage_timing_qpc_ready = 1;
    }

    if (!QueryPerformanceCounter(&counter))
    {
        return omp_get_wtime();
    }

    return (double)counter.QuadPart / (double)mlv_stage_timing_qpc_freq.QuadPart;
#elif defined(CLOCK_MONOTONIC)
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0)
    {
        return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
    }
    return omp_get_wtime();
#else
    return omp_get_wtime();
#endif
}

static inline uint64_t mlv_stage_timing_now_ns(void)
{
    return (uint64_t)(mlv_stage_timing_now() * 1000000000.0);
}

static inline void mlv_stage_timing_reset_snapshot(void)
{
    memset(&g_mlv_stage_timing_snapshot, 0, sizeof(g_mlv_stage_timing_snapshot));
}

static inline void mlv_stage_timing_note_snapshot(const char * stage, uint64_t frameIndex, double elapsed_ms)
{
    if (!stage || !stage[0])
    {
        return;
    }

    g_mlv_stage_timing_snapshot.frame_index = frameIndex;
    g_mlv_stage_timing_snapshot.has_frame_index = 1;

    for (int index = 0; index < MLV_STAGE_TIMING_SNAPSHOT_CAPACITY; ++index)
    {
        mlv_stage_timing_snapshot_entry_t * entry = &g_mlv_stage_timing_snapshot.entries[index];
        if (entry->used && strcmp(entry->stage, stage) == 0)
        {
            entry->elapsed_ms = elapsed_ms;
            return;
        }
        if (!entry->used)
        {
            strncpy(entry->stage, stage, sizeof(entry->stage) - 1);
            entry->stage[sizeof(entry->stage) - 1] = '\0';
            entry->elapsed_ms = elapsed_ms;
            entry->used = 1;
            return;
        }
    }
}

static inline const mlv_stage_timing_snapshot_t * mlv_stage_timing_get_snapshot(void)
{
    return &g_mlv_stage_timing_snapshot;
}

static inline void mlv_stage_timing_note(const char * stage, uint64_t frameIndex, double startTime)
{
    const double elapsed_ms = (mlv_stage_timing_now() - startTime) * 1000.0;
    mlv_stage_timing_note_snapshot(stage, frameIndex, elapsed_ms);
    if (!mlv_stage_timing_enabled()) return;
    FILE * stream = mlv_stage_timing_stream();
    fprintf(stream, "[mlv-stage-timing] frame=%llu stage=%s ms=%.3f\n",
            (unsigned long long)frameIndex, stage, elapsed_ms);
    fflush(stream);
}

static inline void mlv_stage_timing_note_elapsed(const char * stage, uint64_t frameIndex, double elapsed_ms)
{
    mlv_stage_timing_note_snapshot(stage, frameIndex, elapsed_ms);
    if (!mlv_stage_timing_enabled()) return;
    FILE * stream = mlv_stage_timing_stream();
    fprintf(stream, "[mlv-stage-timing] frame=%llu stage=%s ms=%.3f\n",
            (unsigned long long)frameIndex, stage, elapsed_ms);
    fflush(stream);
}

#ifdef __cplusplus
}
#endif

#endif
