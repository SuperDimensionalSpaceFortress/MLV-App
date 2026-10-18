/* igpu_amaze_debayer_cuda.cu - CUDA backend for the generic AMaZE debayer C ABI.
 *
 * The AMaZE math kernels are reused from the reviewed full-stage probe:
 * tools/gpu/cuda_amaze_debayer_stage_probe.cu. This file adds the production
 * ABI wrapper plus full-frame tile orchestration; it does not change GUI/UX
 * claims by itself.
 */

#include "igpu_amaze_debayer.h"

/* GPU-EVENT153-ROOTCAUSE-2: env-gated per-launch timing (default OFF); the
 * stage probe included below pulls in the same header for its launch helpers. */
#define IGPU_LT_DLL_NAME "amaze"
#include "igpu_launch_timing.cuh"

#define CUDA_AMAZE_DEBAYER_STAGE_PROBE_LIBRARY 1
#define CUDA_AMAZE_DEBAYER_STAGE_PROBE_NO_MAIN 1
#include "../cuda_amaze_debayer_stage_probe.cu"

#include <atomic>
#include <chrono>
#include <thread>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <utility>
#include <vector>
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
#include <GL/gl.h>
#include <cuda_gl_interop.h>

#ifndef GL_TEXTURE_2D
#define GL_TEXTURE_2D 0x0DE1
#endif

struct CachedGlImageResource
{
    cudaGraphicsResource * resource = nullptr;
    unsigned int texture = 0;
    int width = 0;
    int height = 0;
    unsigned int flags = 0;
};

/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row K: the clock kick's mapped pinned block (< 2 KB). The host
 * writes stop; the kernel writes the rest, then done last. */
struct ClockKickShared
{
    volatile unsigned int stop;        /* 0 run, 1 end(), 2 a live run's drain */
    volatile unsigned int done;
    volatile unsigned int count;
    volatile unsigned int exitReason;  /* 0 deadline, else the stop value the kernel saw */
    volatile unsigned long long startNs;
    volatile unsigned long long endNs;
    volatile unsigned long long nextSampleNs;   /* carried from one chunk launch to the next */
    volatile unsigned int chunksDone;           /* kernel: chunks finished (they run in order on the kick stream) */
    volatile unsigned int expired;              /* kernel: a chunk saw the kick's deadline */
    volatile unsigned long long sampleNs[IGPU_CLOCK_KICK_MAX_SAMPLES];
    volatile long long sampleClk[IGPU_CLOCK_KICK_MAX_SAMPLES];
    volatile unsigned int sampleSm[IGPU_CLOCK_KICK_MAX_SAMPLES];   /* clock64 is per SM: %smid of each sample */
};

struct igpu_amaze_debayer_backend
{
    std::string description;
    /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: igpu_amaze_debayer_arm_probe / last_probe. */
    int probeArmedCalls = 0;
    igpu_amaze_probe_t lastProbe{};
    unsigned long long * probeHost = nullptr;
    unsigned long long * probeDevice = nullptr;
    cudaEvent_t probeStart = nullptr;
    cudaEvent_t probeMark = nullptr;
    cudaEvent_t probeEnd = nullptr;
    /* Row K: igpu_amaze_debayer_clock_kick_begin / end. */
    cudaStream_t kickStream = nullptr;
    ClockKickShared * kickHost = nullptr;
    ClockKickShared * kickDevice = nullptr;
    bool kickLaunched = false;   // launched and not yet collected
    std::thread kickFeeder;      // submits the kick's chunks, at most kClockKickChunksInFlight queued at a time
    igpu_amaze_debayer_timing_t lastTiming;
    uint16_t * liveReconBayer16 = nullptr;
    float * liveRawFloat = nullptr;
    uint16_t * liveRgb16 = nullptr;
    uint16_t * liveRgba16 = nullptr;
    /* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: the reduced H-Nyquist filter's output,
     * sized with liveRgb16 in ensure_live_texture_buffers. */
    uint16_t * liveRgb16Hnyquist = nullptr;
    /* Sticky; igpu_amaze_debayer_set_reduced_hnyquist. */
    int reducedHnyquist = 0;
    DeviceBuffers liveDeviceBuffers;
    std::vector<DeviceBuffers> liveTileDeviceBuffers;
    std::vector<cudaStream_t> liveTileStreams;
    CachedGlImageResource liveInputR16Resource;
    CachedGlImageResource liveOutputRgba16Resource;
    std::size_t livePixelCount = 0;
    int liveWidth = 0;
    int liveHeight = 0;
    bool liveDeviceBuffersAllocated = false;
};

namespace
{
constexpr int kDefaultLiveTileStreamCount = 64;
constexpr int kMaxLiveTileStreamCount = 64;

int live_tile_stream_count_from_env()
{
    const char * value = std::getenv("MLVAPP_CUDA_AMAZE_LIVE_TILE_STREAMS");
    if (!value || !*value) return kDefaultLiveTileStreamCount;

    char * end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (end == value) return kDefaultLiveTileStreamCount;
    if (parsed < 1) return 1;
    if (parsed > kMaxLiveTileStreamCount) return kMaxLiveTileStreamCount;
    return static_cast<int>(parsed);
}

bool live_fast_launch_checks_from_env()
{
    const char * value = std::getenv("MLVAPP_CUDA_AMAZE_LIVE_FAST_LAUNCH_CHECKS");
    if (!value || !*value) return true;

    const char first = value[0];
    if (first == '0' || first == 'f' || first == 'F'
        || first == 'n' || first == 'N')
    {
        return false;
    }
    if ((first == 'o' || first == 'O')
        && (value[1] == 'f' || value[1] == 'F'))
    {
        return false;
    }
    return true;
}

bool live_direct_rgba_store_from_env()
{
    const char * value = std::getenv("MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE");
    if (!value || !*value) return true;

    const char first = value[0];
    if (first == '0' || first == 'f' || first == 'F'
        || first == 'n' || first == 'N')
    {
        return false;
    }
    if ((first == 'o' || first == 'O')
        && (value[1] == 'f' || value[1] == 'F'))
    {
        return false;
    }
    return true;
}

double now_ms()
{
    using clock = std::chrono::steady_clock;
    const auto now = clock::now().time_since_epoch();
    return std::chrono::duration<double, std::milli>(now).count();
}

/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1: the device's ns timer (no NVML; it runs at a fixed rate whatever
 * the SM clock). clock64 over it is the SM clock. */
__device__ __forceinline__ unsigned long long igpu_globaltimer_ns()
{
    unsigned long long t;
    asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(t));
    return t;
}

/* Row 0: one thread spins spinNs on %globaltimer; out[0] = ns spanned, out[1] = SM cycles over it. */
__global__ void k_sm_clock_probe(unsigned long long * out, unsigned long long spinNs)
{
    if (blockIdx.x != 0 || threadIdx.x != 0) return;
    const unsigned long long t0 = igpu_globaltimer_ns();
    const long long c0 = clock64();
    unsigned long long t1 = t0;
    long long c1 = c0;
    do
    {
        t1 = igpu_globaltimer_ns();
        c1 = clock64();
    } while (t1 - t0 < spinNs);
    out[0] = t1 - t0;
    out[1] = static_cast<unsigned long long>(c1 - c0);
}

__device__ __forceinline__ unsigned int igpu_smid()
{
    unsigned int id;
    asm volatile("mov.u32 %0, %%smid;" : "=r"(id));
    return id;
}

/* Row K: one CHUNK of the kick. A host feeder thread (clock_kick_feeder) keeps at most kClockKickChunksInFlight of
 * these queued on the kick stream, so no single launch exceeds the 100 ms per-launch budget
 * (GPU-LAUNCH-BUDGET-TILING-1, IGPU_LT_LAUNCH_BUDGET_MS), and any other work that has to wait for the device's queued
 * work waits for at most those few chunks, never for the whole kick. Lane 0 spins for at most chunkNs, until maxNs
 * after the FIRST chunk's start, or until the host-mapped stop flag is set, sampling (%globaltimer, clock64, %smid)
 * every periodNs and once as it leaves (at most IGPU_CLOCK_KICK_MAX_SAMPLES over the whole kick). clock64 is per SM and
 * a chunk may run on another SM, so the host only reads an interval whose two samples share an SM. A chunk that starts
 * after a stop or past the deadline returns at once. Each chunk publishes chunksDone last. */
__global__ void k_clock_kick(ClockKickShared * s,
                             unsigned long long maxNs,
                             unsigned long long chunkNs,
                             unsigned long long periodNs,
                             unsigned int chunk)
{
    if (blockIdx.x != 0 || threadIdx.x != 0) return;
    const unsigned long long tIn = igpu_globaltimer_ns();
    if (chunk == 0)
    {
        s->startNs = tIn;
        s->nextSampleNs = tIn;
    }
    const unsigned long long t0 = s->startNs;
    unsigned int count = s->count;
    unsigned long long next = s->nextSampleNs;
    unsigned int reason = s->exitReason;
    if (reason == 0 && s->stop == 0 && tIn - t0 < maxNs)
    {
        for (;;)
        {
            const unsigned long long t = igpu_globaltimer_ns();
            if (t >= next && count < IGPU_CLOCK_KICK_MAX_SAMPLES)
            {
                s->sampleNs[count] = t;
                s->sampleClk[count] = clock64();
                s->sampleSm[count] = igpu_smid();
                ++count;
                next = t + periodNs;
            }
            const unsigned int stop = s->stop;
            if (stop != 0)
            {
                reason = stop;
                break;
            }
            if (t - t0 >= maxNs)
            {
                s->expired = 1;
                break;
            }
            if (t - tIn >= chunkNs) break;
        }
        const unsigned long long tOut = igpu_globaltimer_ns();
        if (count < IGPU_CLOCK_KICK_MAX_SAMPLES)
        {
            s->sampleNs[count] = tOut;
            s->sampleClk[count] = clock64();
            s->sampleSm[count] = igpu_smid();
            ++count;
        }
        s->count = count;
        s->nextSampleNs = next;
        s->exitReason = reason;
        s->endNs = tOut;
    }
    else if (reason == 0 && s->stop != 0)
    {
        s->exitReason = s->stop;
    }
    else if (tIn - t0 >= maxNs)
    {
        s->expired = 1;
    }
    __threadfence_system();
    s->chunksDone = chunk + 1;
    __threadfence_system();
}

constexpr unsigned long long kProbeSpinNs = 50000ull;
constexpr int kClockKickMaxMsCap = 2000;
constexpr unsigned long long kClockKickPeriodNs = 5000000ull;
/* One kick launch spins at most this long: far under the 100 ms per-launch budget (IGPU_LT_LAUNCH_BUDGET_MS). */
constexpr int kClockKickChunkMs = 10;
/* The feeder keeps at most this many chunks queued: work that must wait for the device's queued work (an implicit
 * synchronisation in another stream or DLL) waits for at most ~20 ms of kick, never the whole kick. */
constexpr unsigned int kClockKickChunksInFlight = 2;
constexpr unsigned int kClockKickMaxChunks = (kClockKickMaxMsCap + kClockKickChunkMs - 1) / kClockKickChunkMs + 1;
constexpr double kClockKickDrainWaitMs = 50.0;
constexpr double kClockKickEndWaitMs = 2.0;

/* Row 0: SM MHz on `stream` (launch + sync, host-mapped result), or 0.0 when a step fails. Its codes are noted
 * (the run that called it captures them, or the next run emits them as orphans); it never throws, so a failed
 * probe never fails the run. */
double sm_clock_probe_mhz(igpu_amaze_debayer_backend * backend, cudaStream_t stream)
{
    if (!backend->probeHost)
    {
        void * host = nullptr;
        cudaError_t rc = cudaHostAlloc(&host, 2u * sizeof(unsigned long long), cudaHostAllocMapped);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "probe:host_alloc");
            return 0.0;
        }
        void * device = nullptr;
        rc = cudaHostGetDevicePointer(&device, host, 0);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "probe:device_pointer");
            cudaFreeHost(host);
            return 0.0;
        }
        backend->probeHost = static_cast<unsigned long long *>(host);
        backend->probeDevice = static_cast<unsigned long long *>(device);
    }
    volatile unsigned long long * result = backend->probeHost;
    result[0] = 0;
    result[1] = 0;
    IGPU_LT_LAUNCH("k_sm_clock_probe", 1, 1, stream,
                   k_sm_clock_probe<<<1, 1, 0, stream>>>(backend->probeDevice, kProbeSpinNs));
    cudaError_t rc = cudaPeekAtLastError();
    if (rc == cudaSuccess) rc = IGPU_LT_SYNC_CALL("probe:sm_clock_sync", cudaStreamSynchronize(stream));
    if (rc != cudaSuccess)
    {
        IGPU_CUDA_ERROR_NOTE(rc, "probe:sm_clock");
        return 0.0;
    }
    const unsigned long long ns = result[0];
    const unsigned long long cycles = result[1];
    if (ns == 0) return 0.0;
    return static_cast<double>(cycles) * 1000.0 / static_cast<double>(ns);
}

/* Row 0: the device-time events of one armed batch (see run_frame_to_device_rgb16_bayer16_post_wb_batched). */
struct LiveRunProbe
{
    cudaEvent_t start = nullptr;
    cudaEvent_t mark = nullptr;
    cudaEvent_t end = nullptr;
    bool ok = false;
};

bool ensure_probe_events(igpu_amaze_debayer_backend * backend)
{
    cudaEvent_t * events[] = {&backend->probeStart, &backend->probeMark, &backend->probeEnd};
    for (cudaEvent_t * event : events)
    {
        if (*event) continue;
        const cudaError_t rc = cudaEventCreate(event);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "probe:event_create");
            *event = nullptr;
            return false;
        }
    }
    return true;
}

/* Row 0: an armed run's probe steps. begin (before the kernel_ms window) consumes one armed call, reads
 * mhz_before on tile stream 0 and hands back the events for the batch; finish (after the drain and after the
 * timing fields are set) reads device_ms and mhz_after. lastProbe.valid is 0 for an unarmed run. */
LiveRunProbe * begin_live_run_probe(igpu_amaze_debayer_backend * backend, LiveRunProbe * probe)
{
    backend->lastProbe = igpu_amaze_probe_t{};
    if (backend->probeArmedCalls <= 0 || backend->liveTileStreams.empty()) return nullptr;
    --backend->probeArmedCalls;
    if (!ensure_probe_events(backend)) return nullptr;
    backend->lastProbe.sm_mhz_before = sm_clock_probe_mhz(backend, backend->liveTileStreams[0]);
    probe->start = backend->probeStart;
    probe->mark = backend->probeMark;
    probe->end = backend->probeEnd;
    probe->ok = backend->lastProbe.sm_mhz_before > 0.0;
    return probe;
}

void finish_live_run_probe(igpu_amaze_debayer_backend * backend, const LiveRunProbe * probe)
{
    if (!probe || !probe->ok) return;
    float deviceMs = 0.0f;
    const cudaError_t rc = cudaEventElapsedTime(&deviceMs, probe->start, probe->end);
    if (rc != cudaSuccess)
    {
        IGPU_CUDA_ERROR_NOTE(rc, "probe:elapsed");
        return;
    }
    backend->lastProbe.device_ms = static_cast<double>(deviceMs);
    backend->lastProbe.sm_mhz_after = sm_clock_probe_mhz(backend, backend->liveTileStreams[0]);
    backend->lastProbe.valid = backend->lastProbe.sm_mhz_after > 0.0 ? 1 : 0;
}

/* Row K: a live run's drain ends a running kick (host write only), so the kick never overlaps its CUDA-GL handoff. */
void stop_clock_kick_for_live_run(igpu_amaze_debayer_backend * backend)
{
    if (backend->kickLaunched && backend->kickHost && backend->kickHost->stop == 0)
    {
        backend->kickHost->stop = 2;
    }
}

/* Row K: the kick's host feeder (one thread per kick, started by clock_kick_begin). It keeps at most
 * kClockKickChunksInFlight chunks queued, launching the next as one finishes, until the stop flag, the kick's
 * deadline (a chunk saw it, or the feeder's own clock as a backstop), or a failed launch. Then it waits (bounded) for
 * the chunks it launched to finish and publishes done, which end() reads. It makes no synchronising call. */
void clock_kick_feeder(igpu_amaze_debayer_backend * backend, unsigned long long maxNs)
{
    ClockKickShared * s = backend->kickHost;
    const unsigned long long chunkNs = static_cast<unsigned long long>(kClockKickChunkMs) * 1000000ull;
    const double maxMs = static_cast<double>(maxNs) / 1000000.0;
    const double started = now_ms();
    unsigned int launched = 0;
    while (s->stop == 0 && s->expired == 0 && launched < kClockKickMaxChunks
           && now_ms() - started < maxMs + kClockKickDrainWaitMs)
    {
        if (launched >= s->chunksDone + kClockKickChunksInFlight)
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            continue;
        }
        IGPU_LT_LAUNCH("k_clock_kick", 1, 32, backend->kickStream,
                       k_clock_kick<<<1, 32, 0, backend->kickStream>>>(backend->kickDevice,
                                                                       maxNs,
                                                                       chunkNs,
                                                                       kClockKickPeriodNs,
                                                                       launched));
        const cudaError_t launchRc = cudaGetLastError();
        if (launchRc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(launchRc, "clock_kick:launch");
            break;
        }
        ++launched;
        /* WDDM batches launches: a query submits this chunk now instead of at the next sync. NotReady is the answer. */
        const cudaError_t queryRc = cudaStreamQuery(backend->kickStream);
        if (queryRc != cudaSuccess && queryRc != cudaErrorNotReady)
        {
            IGPU_CUDA_ERROR_NOTE(queryRc, "clock_kick:submit");
            break;
        }
    }
    const double drainStart = now_ms();
    while (s->chunksDone < launched && now_ms() - drainStart < kClockKickDrainWaitMs)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if (s->chunksDone >= launched)
    {
        std::atomic_thread_fence(std::memory_order_release);
        s->done = 1;
    }
}

/* Teardown adds no synchronisation (CudaLaunchTiming.TimingOnAddsNoSynchronisationAtBackendTeardown): a kick still
 * running is told to stop, its feeder joined (it only sleeps and launches; it exits once it sees the flag and its
 * chunks drain, bounded), and one whose chunks have still not finished keeps its mapped block (it is never freed under
 * a running kernel). */
void free_probe_and_kick(igpu_amaze_debayer_backend * backend)
{
    if (backend->kickHost && backend->kickHost->stop == 0) backend->kickHost->stop = 1;
    if (backend->kickFeeder.joinable()) backend->kickFeeder.join();
    if (backend->kickHost)
    {
        ClockKickShared * s = backend->kickHost;
        if (s->stop == 0) s->stop = 1;
        const double waitStart = now_ms();
        while (backend->kickLaunched && !s->done && now_ms() - waitStart < kClockKickEndWaitMs)
        {
        }
        if (!backend->kickLaunched || s->done)
        {
            cudaFreeHost(backend->kickHost);
        }
        backend->kickHost = nullptr;
        backend->kickDevice = nullptr;
    }
    if (backend->kickStream) cudaStreamDestroy(backend->kickStream);
    backend->kickStream = nullptr;
    backend->kickLaunched = false;
    if (backend->probeHost) cudaFreeHost(backend->probeHost);
    backend->probeHost = nullptr;
    backend->probeDevice = nullptr;
    cudaEvent_t * events[] = {&backend->probeStart, &backend->probeMark, &backend->probeEnd};
    for (cudaEvent_t * event : events)
    {
        if (*event) cudaEventDestroy(*event);
        *event = nullptr;
    }
}

int reset_cached_gl_resource(CachedGlImageResource * cache, bool tolerateInvalidGraphicsContext)
{
    int status = 0;
    if (!cache) return status;
    if (cache->resource)
    {
        const cudaError_t rc = cudaGraphicsUnregisterResource(cache->resource);
        if (rc != cudaSuccess)
        {
            /* r8 class sweep: a tolerated code (GL context already gone) is still noted;
             * only its log line stays quiet. */
            IGPU_CUDA_ERROR_NOTE(rc, "gl_cache:unregister");
            if (!(tolerateInvalidGraphicsContext && rc == cudaErrorInvalidGraphicsContext))
            {
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(cached GL texture) failed: %s\n",
                             cudaGetErrorString(rc));
            }
            status = 2;
        }
    }
    cache->resource = nullptr;
    cache->texture = 0;
    cache->width = 0;
    cache->height = 0;
    cache->flags = 0;
    return status;
}

void reset_live_gl_resources(igpu_amaze_debayer_backend * backend)
{
    if (!backend) return;
    reset_cached_gl_resource(&backend->liveInputR16Resource, true);
    reset_cached_gl_resource(&backend->liveOutputRgba16Resource, true);
}

int ensure_cached_gl_resource(CachedGlImageResource * cache,
                              unsigned int glTexture,
                              int width,
                              int height,
                              unsigned int flags,
                              const char * label)
{
    if (!cache || glTexture == 0 || width <= 0 || height <= 0) return -1;
    if (cache->resource
        && cache->texture == glTexture
        && cache->width == width
        && cache->height == height
        && cache->flags == flags)
    {
        return 0;
    }

    const int resetRc = reset_cached_gl_resource(cache, false);
    if (resetRc != 0) return resetRc;
    cudaGraphicsResource * resource = nullptr;
    const cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                       glTexture,
                                                       GL_TEXTURE_2D,
                                                       flags);
    if (rc != cudaSuccess)
    {
        IGPU_CUDA_ERROR_NOTE(rc, "gl_cache:register");
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(%s) failed: %s\n",
                     label ? label : "cached GL texture",
                     cudaGetErrorString(rc));
        return 2;
    }

    cache->resource = resource;
    cache->texture = glTexture;
    cache->width = width;
    cache->height = height;
    cache->flags = flags;
    return 0;
}

__global__ void k_tile_load_float(const float * raw,
                                  float * cfa,
                                  float * rgbgreen,
                                  int width,
                                  int height,
                                  int top,
                                  int left,
                                  int rr1,
                                  int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr >= rr1 || cc >= cc1) return;

    const int row = reflect_index(top + rr, height);
    const int col = reflect_index(left + cc, width);
    const int idx = rr * kTileSize + cc;
    const float normalized =
        raw[static_cast<std::size_t>(row) * width + col] / 65535.0f;
    cfa[idx] = normalized;
    rgbgreen[idx] = (fc_rggb(rr, cc) == 1) ? normalized : 0.0f;
}

__device__ uint16_t truncate_amaze_float_to_u16(float value)
{
    if (!(value > 0.0f)) return 0;
    if (!(value < 65535.0f)) return 65535;
    return static_cast<uint16_t>(static_cast<uint32_t>(value));
}

__global__ void k_store_tile_rgb16(const float * red,
                                   const float * green,
                                   const float * blue,
                                   uint16_t * outRgb16,
                                   int width,
                                   int height,
                                   int top,
                                   int left,
                                   int rr1,
                                   int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 16 || rr >= rr1 - 16 || cc < 16 || cc >= cc1 - 16) return;

    const int row = top + rr;
    const int col = left + cc;
    if (row < 0 || row >= height || col < 0 || col >= width) return;

    const int tileIndex = rr * kTileSize + cc;
    const std::size_t outIndex =
        (static_cast<std::size_t>(row) * width + col) * 3u;
    outRgb16[outIndex + 0] = truncate_amaze_float_to_u16(red[tileIndex]);
    outRgb16[outIndex + 1] = truncate_amaze_float_to_u16(green[tileIndex]);
    outRgb16[outIndex + 2] = truncate_amaze_float_to_u16(blue[tileIndex]);
}

__global__ void k_pack_rgb16_to_rgba16(const uint16_t * rgb,
                                       uint16_t * rgba,
                                       std::size_t pixelCount)
{
    const std::size_t pixel =
        static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (pixel >= pixelCount) return;

    rgba[pixel * 4u + 0u] = rgb[pixel * 3u + 0u];
    rgba[pixel * 4u + 1u] = rgb[pixel * 3u + 1u];
    rgba[pixel * 4u + 2u] = rgb[pixel * 3u + 2u];
    rgba[pixel * 4u + 3u] = 65535u;
}

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: line-for-line port of
 * debayer_reduced_hnyquist121_rgb16 (src/debayer/debayer.c). One thread per pixel;
 * out and in never alias. */
__global__ void k_reduced_hnyquist121(const uint16_t * in,
                                      uint16_t * out,
                                      int w,
                                      int h)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;

    const uint16_t * src = in + static_cast<std::size_t>(y) * static_cast<std::size_t>(w) * 3u;
    uint16_t * dst = out + static_cast<std::size_t>(y) * static_cast<std::size_t>(w) * 3u;
    const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
    const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
    for (int c = 0; c < 3; ++c)
    {
        const uint32_t l = src[static_cast<std::size_t>(xl) * 3u + static_cast<std::size_t>(c)];
        const uint32_t m = src[static_cast<std::size_t>(x) * 3u + static_cast<std::size_t>(c)];
        const uint32_t r = src[static_cast<std::size_t>(xr) * 3u + static_cast<std::size_t>(c)];
        dst[static_cast<std::size_t>(x) * 3u + static_cast<std::size_t>(c)] =
            static_cast<uint16_t>((l + 2u * m + r + 2u) >> 2);
    }
}

void launch_reduced_hnyquist121(const uint16_t * in, uint16_t * out, int w, int h)
{
    const dim3 block(32, 8);
    const dim3 grid((w + block.x - 1) / block.x, (h + block.y - 1) / block.y);
    IGPU_LT_LAUNCH("k_reduced_hnyquist121", grid, block, 0,
                   k_reduced_hnyquist121<<<grid, block>>>(in, out, w, h));
}

__device__ __forceinline__ uint16_t clamp_double_to_u16(double value)
{
    if (value <= 0.0) return 0u;
    if (value >= 65535.0) return 65535u;
    return static_cast<uint16_t>(value);
}

__global__ void k_pack_rgb16_to_rgba16_post_wb_undo(const uint16_t * rgb,
                                                    uint16_t * rgba,
                                                    std::size_t pixelCount,
                                                    int blackLevel,
                                                    double wbMultiplierR,
                                                    double wbMultiplierG,
                                                    double wbMultiplierB)
{
    const std::size_t pixel =
        static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (pixel >= pixelCount) return;

    const std::size_t rgbIndex = pixel * 3u;
    const std::size_t rgbaIndex = pixel * 4u;
    rgba[rgbaIndex + 0u] =
        clamp_double_to_u16((static_cast<double>(rgb[rgbIndex + 0u]) / wbMultiplierR)
                            + static_cast<double>(blackLevel));
    rgba[rgbaIndex + 1u] =
        clamp_double_to_u16((static_cast<double>(rgb[rgbIndex + 1u]) / wbMultiplierG)
                            + static_cast<double>(blackLevel));
    rgba[rgbaIndex + 2u] =
        clamp_double_to_u16((static_cast<double>(rgb[rgbIndex + 2u]) / wbMultiplierB)
                            + static_cast<double>(blackLevel));
    rgba[rgbaIndex + 3u] = 65535u;
}

__global__ void k_store_tile_rgba16_post_wb_undo(const float * red,
                                                 const float * green,
                                                 const float * blue,
                                                 uint16_t * outRgba16,
                                                 int width,
                                                 int height,
                                                 int top,
                                                 int left,
                                                 int rr1,
                                                 int cc1,
                                                 int blackLevel,
                                                 double wbMultiplierR,
                                                 double wbMultiplierG,
                                                 double wbMultiplierB)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 16 || rr >= rr1 - 16 || cc < 16 || cc >= cc1 - 16) return;

    const int row = top + rr;
    const int col = left + cc;
    if (row < 0 || row >= height || col < 0 || col >= width) return;

    const int tileIndex = rr * kTileSize + cc;
    const std::size_t outIndex =
        (static_cast<std::size_t>(row) * width + col) * 4u;
    const uint16_t r = truncate_amaze_float_to_u16(red[tileIndex]);
    const uint16_t g = truncate_amaze_float_to_u16(green[tileIndex]);
    const uint16_t b = truncate_amaze_float_to_u16(blue[tileIndex]);
    outRgba16[outIndex + 0u] =
        clamp_double_to_u16((static_cast<double>(r) / wbMultiplierR)
                            + static_cast<double>(blackLevel));
    outRgba16[outIndex + 1u] =
        clamp_double_to_u16((static_cast<double>(g) / wbMultiplierG)
                            + static_cast<double>(blackLevel));
    outRgba16[outIndex + 2u] =
        clamp_double_to_u16((static_cast<double>(b) / wbMultiplierB)
                            + static_cast<double>(blackLevel));
    outRgba16[outIndex + 3u] = 65535u;
}

__device__ __forceinline__ int fc_rggb_absolute(int row, int col)
{
    const int row2 = row & 1;
    const int col2 = col & 1;
    if (row2 == 0 && col2 == 0) return 0;
    if (row2 == 1 && col2 == 1) return 2;
    return 1;
}

__global__ void k_bayer16_to_post_wb_raw_float(const uint16_t * bayer16,
                                               float * rawFloat,
                                               std::size_t pixelCount,
                                               int width,
                                               int blackLevel,
                                               double wbMultiplierR,
                                               double wbMultiplierG,
                                               double wbMultiplierB)
{
    const std::size_t pixel =
        static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (pixel >= pixelCount) return;

    const int row = static_cast<int>(pixel / static_cast<std::size_t>(width));
    const int col = static_cast<int>(pixel - static_cast<std::size_t>(row) * width);
    const int channel = fc_rggb_absolute(row, col);
    const double wb =
        (channel == 0) ? wbMultiplierR : ((channel == 2) ? wbMultiplierB : wbMultiplierG);
    rawFloat[pixel] =
        static_cast<float>((static_cast<double>(bayer16[pixel]) - static_cast<double>(blackLevel)) * wb);
}

__global__ void k_tile_load_bayer16_post_wb(const uint16_t * bayer16,
                                            float * cfa,
                                            float * rgbgreen,
                                            int width,
                                            int height,
                                            int top,
                                            int left,
                                            int rr1,
                                            int cc1,
                                            int blackLevel,
                                            double wbMultiplierR,
                                            double wbMultiplierG,
                                            double wbMultiplierB)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr >= rr1 || cc >= cc1) return;

    const int row = reflect_index(top + rr, height);
    const int col = reflect_index(left + cc, width);
    const int channel = fc_rggb_absolute(row, col);
    const double wb =
        (channel == 0) ? wbMultiplierR : ((channel == 2) ? wbMultiplierB : wbMultiplierG);
    const std::size_t sourceIndex =
        static_cast<std::size_t>(row) * static_cast<std::size_t>(width)
        + static_cast<std::size_t>(col);
    const float normalized =
        static_cast<float>(
            (static_cast<double>(bayer16[sourceIndex]) - static_cast<double>(blackLevel)) * wb)
        / 65535.0f;
    const int idx = rr * kTileSize + cc;
    cfa[idx] = normalized;
    rgbgreen[idx] = (fc_rggb(rr, cc) == 1) ? normalized : 0.0f;
}

void free_live_texture_buffers(igpu_amaze_debayer_backend * backend)
{
    if (!backend) return;
    reset_live_gl_resources(backend);
    cudaFree(backend->liveReconBayer16);
    cudaFree(backend->liveRawFloat);
    cudaFree(backend->liveRgb16);
    cudaFree(backend->liveRgba16);
    cudaFree(backend->liveRgb16Hnyquist);
    backend->liveRgb16Hnyquist = nullptr;
    backend->liveReconBayer16 = nullptr;
    backend->liveRawFloat = nullptr;
    backend->liveRgb16 = nullptr;
    backend->liveRgba16 = nullptr;
    if (backend->liveDeviceBuffersAllocated)
    {
        free_device(&backend->liveDeviceBuffers);
        backend->liveDeviceBuffers = DeviceBuffers();
        for (std::size_t index = 0; index < backend->liveTileDeviceBuffers.size(); ++index)
        {
            free_device(&backend->liveTileDeviceBuffers[index]);
        }
        backend->liveTileDeviceBuffers.clear();
        for (std::size_t index = 0; index < backend->liveTileStreams.size(); ++index)
        {
            if (backend->liveTileStreams[index])
            {
                cudaStreamDestroy(backend->liveTileStreams[index]);
            }
        }
        backend->liveTileStreams.clear();
        backend->liveDeviceBuffersAllocated = false;
    }
    backend->livePixelCount = 0;
    backend->liveWidth = 0;
    backend->liveHeight = 0;
}

void ensure_live_texture_buffers(igpu_amaze_debayer_backend * backend,
                                 int width,
                                 int height)
{
    if (!backend || width <= 0 || height <= 0)
    {
        throw CudaStageProbeError("invalid live texture buffer geometry");
    }

    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const int streamCount = live_tile_stream_count_from_env();
    if (backend->liveReconBayer16
        && backend->liveRawFloat
        && backend->liveRgb16
        && backend->liveRgba16
        && backend->liveRgb16Hnyquist
        && backend->liveDeviceBuffersAllocated
        && backend->livePixelCount == pixelCount
        && backend->liveWidth == width
        && backend->liveHeight == height
        && backend->liveTileStreams.size() == static_cast<std::size_t>(streamCount))
    {
        return;
    }

    free_live_texture_buffers(backend);
    CK(cudaMalloc(&backend->liveReconBayer16, pixelCount * sizeof(uint16_t)));
    CK(cudaMalloc(&backend->liveRawFloat, pixelCount * sizeof(float)));
    CK(cudaMalloc(&backend->liveRgb16, pixelCount * 3u * sizeof(uint16_t)));
    CK(cudaMalloc(&backend->liveRgba16, pixelCount * 4u * sizeof(uint16_t)));
    CK(cudaMalloc(&backend->liveRgb16Hnyquist, pixelCount * 3u * sizeof(uint16_t)));
    allocate_device(&backend->liveDeviceBuffers, 1);
    backend->liveTileDeviceBuffers.resize(static_cast<std::size_t>(streamCount));
    backend->liveTileStreams.resize(static_cast<std::size_t>(streamCount), nullptr);
    for (int index = 0; index < streamCount; ++index)
    {
        allocate_device(&backend->liveTileDeviceBuffers[static_cast<std::size_t>(index)], 1);
        CK(cudaStreamCreateWithFlags(
            &backend->liveTileStreams[static_cast<std::size_t>(index)],
            cudaStreamNonBlocking));
    }
    backend->liveDeviceBuffersAllocated = true;
    backend->livePixelCount = pixelCount;
    backend->liveWidth = width;
    backend->liveHeight = height;
}

int copy_gl_r16_texture_to_bayer16(unsigned int glTexture,
                                   uint16_t * dBayer16,
                                   int width,
                                   int height,
                                   CachedGlImageResource * cache = nullptr)
{
    if (glTexture == 0 || !dBayer16 || width <= 0 || height <= 0) return -1;

    cudaGraphicsResource * resource = nullptr;
    const bool cached = cache != nullptr;
    if (cached)
    {
        const int cacheRc = ensure_cached_gl_resource(cache,
                                                      glTexture,
                                                      width,
                                                      height,
                                                      cudaGraphicsRegisterFlagsReadOnly,
                                                      "cached GL_R16 source texture");
        if (cacheRc != 0) return cacheRc;
        resource = cache->resource;
    }
    else
    {
        cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                     glTexture,
                                                     GL_TEXTURE_2D,
                                                     cudaGraphicsRegisterFlagsReadOnly);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "gl_r16_in:register");
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(rc));
            return 2;
        }
    }

    cudaError_t rc = IGPU_LT_SYNC_CALL("gl_r16_in:map", cudaGraphicsMapResources(1, &resource, 0));
    if (rc != cudaSuccess)
    {
        /* r8 (sol r7 blocker): an uncached resource's unregister code is noted too, at its own
         * site, before the return (the cached reset notes its own). */
        const cudaError_t unregisterRc =
            cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
        const igpu_cuda_error_slot::Evaluated mapFailure[] = {
            {(int)rc, "gl_r16_in:map"},
            {(int)unregisterRc, "gl_r16_in:unregister"},
        };
        IGPU_CUDA_ERROR_NOTE_EVALUATED(mapFailure);
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_R16 source texture) failed: %s\n",
                     cudaGetErrorString(rc));
        if (unregisterRc != cudaSuccess)
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(unregisterRc));
        if (cached) reset_cached_gl_resource(cache, false);
        return 2;
    }

    cudaArray_t mappedArray = nullptr;
    /* r8 class sweep: the mapped-array code keeps its own site (it was noted as the copy's). */
    const cudaError_t arrayRc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
    if (arrayRc == cudaSuccess)
    {
        const std::size_t rowBytes = static_cast<std::size_t>(width) * sizeof(uint16_t);
        rc = IGPU_LT_SYNC_CALL("gl_r16_in:copy_from_array",
                               cudaMemcpy2DFromArray(dBayer16,
                                                     rowBytes,
                                                     mappedArray,
                                                     0,
                                                     0,
                                                     rowBytes,
                                                     static_cast<std::size_t>(height),
                                                     cudaMemcpyDeviceToDevice));
    }

    const cudaError_t unmapRc =
        IGPU_LT_SYNC_CALL("gl_r16_in:unmap", cudaGraphicsUnmapResources(1, &resource, 0));
    const cudaError_t unregisterRc =
        cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
    if (arrayRc != cudaSuccess || rc != cudaSuccess || unmapRc != cudaSuccess || unregisterRc != cudaSuccess)
    {
        /* r7: all evaluated codes are noted, each at its own site, before the return. */
        const igpu_cuda_error_slot::Evaluated evaluated[] = {
            {(int)arrayRc, "gl_r16_in:mapped_array"},
            {(int)rc, "gl_r16_in:copy_from_array"},
            {(int)unmapRc, "gl_r16_in:unmap"},
            {(int)unregisterRc, "gl_r16_in:unregister"},
        };
        IGPU_CUDA_ERROR_NOTE_EVALUATED(evaluated);
        if (arrayRc != cudaSuccess)
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsSubResourceGetMappedArray(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(arrayRc));
        if (rc != cudaSuccess)
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] GL_R16 source texture device copy failed: %s\n",
                         cudaGetErrorString(rc));
        if (unmapRc != cudaSuccess)
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(unmapRc));
        if (unregisterRc != cudaSuccess)
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(unregisterRc));
        /* unregisterRc fails only uncached, so this resets as before. */
        if (cached) reset_cached_gl_resource(cache, false);
        return 2;
    }
    return 0;
}

void clear_nyquist_dependent_scratch(const DeviceBuffers & d,
                                     cudaStream_t stream);

void run_tile(const float * dRaw,
              uint16_t * dRgb16,
              DeviceBuffers & d,
              int width,
              int height,
              int top,
              int left,
              cudaStream_t stream = 0)
{
    const int bottom = imin_i(top + kTileSize, height + 16);
    const int right = imin_i(left + kTileSize, width + 16);
    const int rr1 = bottom - top;
    const int cc1 = right - left;

    clear_device_stages(d);
    igpu_lt::TileScope lt_tile(top, left);

    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);

    IGPU_LT_LAUNCH("k_tile_load_float", grid, block, stream,
                   k_tile_load_float<<<grid, block, 0, stream>>>(dRaw,
                                                                 d.cfa,
                                                                 d.rgbgreen,
                                                                 width,
                                                                 height,
                                                                 top,
                                                                 left,
                                                                 rr1,
                                                                 cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_gradients", grid, block, stream,
                   k_gradients<<<grid, block, 0, stream>>>(d.cfa, d.dirwts0, d.dirwts1, d.delhvsqsum, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_diagonal_precursors", grid, block, stream,
                   k_diagonal_precursors<<<grid, block, 0, stream>>>(d.cfa, d.delp, d.delm, d.dgrbsq1p, d.dgrbsq1m, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_green_interpolation", grid, block, stream,
                   k_green_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                                     d.dirwts0,
                                                                     d.dirwts1,
                                                                     d.vcd,
                                                                     d.hcd,
                                                                     d.vcdalt,
                                                                     d.hcdalt,
                                                                     d.dgintv,
                                                                     d.dginth,
                                                                     rr1,
                                                                     cc1));
    CK(cudaGetLastError());
    launch_variance_selection_prefix(d, rr1, cc1, stream);
    IGPU_LT_LAUNCH("k_hvwt_adaptive_weights", grid, block, stream,
                   k_hvwt_adaptive_weights<<<grid, block, 0, stream>>>(d.dirwts0,
                                                                       d.dirwts1,
                                                                       d.vcd,
                                                                       d.hcd,
                                                                       d.dgintv,
                                                                       d.dginth,
                                                                       d.hvwt,
                                                                       rr1,
                                                                       cc1));
    CK(cudaGetLastError());
    clear_nyquist_dependent_scratch(d, stream);
    IGPU_LT_LAUNCH("k_nyquist_test", grid, block, stream,
                   k_nyquist_test<<<grid, block, 0, stream>>>(d.cddiffsq, d.delhvsqsum, d.nyquist, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_nyquist_refine_row_prefix", 1, kNyquistPrefixThreads, stream,
                   k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads, 0, stream>>>(d.nyquist, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_nyquist_area_interpolation", grid, block, stream,
                   k_nyquist_area_interpolation<<<grid, block, 0, stream>>>(d.cfa, d.nyquist, d.hvwt, rr1, cc1));
    CK(cudaGetLastError());
    launch_green_plane_assembly(grid,
                                block,
                                d.cfa,
                                d.vcd,
                                d.hcd,
                                d.nyquist,
                                d.hvwt,
                                d.dgrb0,
                                d.rgbgreen,
                                d.dgrb2h,
                                d.dgrb2v,
                                rr1,
                                cc1,
                                stream);
    IGPU_LT_LAUNCH("k_nyquist_green_refinement", grid, block, stream,
                   k_nyquist_green_refinement<<<grid, block, 0, stream>>>(d.cfa,
                                                                          d.vcd,
                                                                          d.hcd,
                                                                          d.nyquist,
                                                                          d.dgrb2h,
                                                                          d.dgrb2v,
                                                                          d.dgrb0,
                                                                          d.rgbgreen,
                                                                          rr1,
                                                                          cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_diagonal_rb_interpolation", grid, block, stream,
                   k_diagonal_rb_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                                           d.delp,
                                                                           d.delm,
                                                                           d.dgrbsq1p,
                                                                           d.dgrbsq1m,
                                                                           d.rbm,
                                                                           d.rbp,
                                                                           d.pmwt,
                                                                           rr1,
                                                                           cc1));
    CK(cudaGetLastError());
    launch_pmwt_refinement_and_rbint(grid,
                                     block,
                                     d.cfa,
                                     d.rbm,
                                     d.rbp,
                                     d.pmwt,
                                     d.pmwtalt,
                                     d.rbint,
                                     rr1,
                                     cc1,
                                     stream);
    IGPU_LT_LAUNCH("k_diagonal_green_correction", grid, block, stream,
                   k_diagonal_green_correction<<<grid, block, 0, stream>>>(d.cfa,
                                                                           d.dirwts0,
                                                                           d.dirwts1,
                                                                           d.hvwt,
                                                                           d.pmwt,
                                                                           d.rbint,
                                                                           d.dgrb0,
                                                                           d.rgbgreen,
                                                                           rr1,
                                                                           cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_chrominance_coset_split", grid, block, stream,
                   k_chrominance_coset_split<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_fancy_chrominance_interpolation", grid, block, stream,
                   k_fancy_chrominance_interpolation<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_final_output_planes", grid, block, stream,
                   k_final_output_planes<<<grid, block, 0, stream>>>(d.rgbgreen,
                                                                     d.hvwt,
                                                                     d.dgrb0,
                                                                     d.dgrb1,
                                                                     d.red,
                                                                     d.green,
                                                                     d.blue,
                                                                     rr1,
                                                                     cc1));
    CK(cudaGetLastError());
    IGPU_LT_LAUNCH("k_store_tile_rgb16", grid, block, stream,
                   k_store_tile_rgb16<<<grid, block, 0, stream>>>(d.red,
                                                                  d.green,
                                                                  d.blue,
                                                                  dRgb16,
                                                                  width,
                                                                  height,
                                                                  top,
                                                                  left,
                                                                  rr1,
                                                                  cc1));
    CK(cudaGetLastError());
}

void run_tile_bayer16_post_wb(const uint16_t * dBayer16,
                              uint16_t * dRgb16,
                              uint16_t * dRgba16,
                              DeviceBuffers & d,
                              int width,
                              int height,
                              int top,
                              int left,
                              int blackLevel,
                              double wbMultiplierR,
                              double wbMultiplierG,
                              double wbMultiplierB,
                              cudaStream_t stream = 0,
                              bool fastLaunchChecks = false)
{
    const int bottom = imin_i(top + kTileSize, height + 16);
    const int right = imin_i(left + kTileSize, width + 16);
    const int rr1 = bottom - top;
    const int cc1 = right - left;

    clear_device_stages(d);
    igpu_lt::TileScope lt_tile(top, left);

    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);

#define CHECK_LIVE_TILE_LAUNCH() \
    do { \
        if (!fastLaunchChecks) CK(cudaGetLastError()); \
    } while (0)

    IGPU_LT_LAUNCH("k_tile_load_bayer16_post_wb", grid, block, stream,
                   k_tile_load_bayer16_post_wb<<<grid, block, 0, stream>>>(dBayer16,
                                                                           d.cfa,
                                                                           d.rgbgreen,
                                                                           width,
                                                                           height,
                                                                           top,
                                                                           left,
                                                                           rr1,
                                                                           cc1,
                                                                           blackLevel,
                                                                           wbMultiplierR,
                                                                           wbMultiplierG,
                                                                           wbMultiplierB));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_gradients", grid, block, stream,
                   k_gradients<<<grid, block, 0, stream>>>(d.cfa, d.dirwts0, d.dirwts1, d.delhvsqsum, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_diagonal_precursors", grid, block, stream,
                   k_diagonal_precursors<<<grid, block, 0, stream>>>(d.cfa, d.delp, d.delm, d.dgrbsq1p, d.dgrbsq1m, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_green_interpolation", grid, block, stream,
                   k_green_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                                     d.dirwts0,
                                                                     d.dirwts1,
                                                                     d.vcd,
                                                                     d.hcd,
                                                                     d.vcdalt,
                                                                     d.hcdalt,
                                                                     d.dgintv,
                                                                     d.dginth,
                                                                     rr1,
                                                                     cc1));
    CHECK_LIVE_TILE_LAUNCH();
    launch_variance_selection_prefix(d, rr1, cc1, stream, !fastLaunchChecks);
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_hvwt_adaptive_weights", grid, block, stream,
                   k_hvwt_adaptive_weights<<<grid, block, 0, stream>>>(d.dirwts0,
                                                                       d.dirwts1,
                                                                       d.vcd,
                                                                       d.hcd,
                                                                       d.dgintv,
                                                                       d.dginth,
                                                                       d.hvwt,
                                                                       rr1,
                                                                       cc1));
    CHECK_LIVE_TILE_LAUNCH();
    clear_nyquist_dependent_scratch(d, stream);
    IGPU_LT_LAUNCH("k_nyquist_test", grid, block, stream,
                   k_nyquist_test<<<grid, block, 0, stream>>>(d.cddiffsq, d.delhvsqsum, d.nyquist, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_nyquist_refine_row_prefix", 1, kNyquistPrefixThreads, stream,
                   k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads, 0, stream>>>(d.nyquist, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_nyquist_area_interpolation", grid, block, stream,
                   k_nyquist_area_interpolation<<<grid, block, 0, stream>>>(d.cfa, d.nyquist, d.hvwt, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    launch_green_plane_assembly(grid,
                                block,
                                d.cfa,
                                d.vcd,
                                d.hcd,
                                d.nyquist,
                                d.hvwt,
                                d.dgrb0,
                                d.rgbgreen,
                                d.dgrb2h,
                                d.dgrb2v,
                                rr1,
                                cc1,
                                stream,
                                !fastLaunchChecks);
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_nyquist_green_refinement", grid, block, stream,
                   k_nyquist_green_refinement<<<grid, block, 0, stream>>>(d.cfa,
                                                                          d.vcd,
                                                                          d.hcd,
                                                                          d.nyquist,
                                                                          d.dgrb2h,
                                                                          d.dgrb2v,
                                                                          d.dgrb0,
                                                                          d.rgbgreen,
                                                                          rr1,
                                                                          cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_diagonal_rb_interpolation", grid, block, stream,
                   k_diagonal_rb_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                                           d.delp,
                                                                           d.delm,
                                                                           d.dgrbsq1p,
                                                                           d.dgrbsq1m,
                                                                           d.rbm,
                                                                           d.rbp,
                                                                           d.pmwt,
                                                                           rr1,
                                                                           cc1));
    CHECK_LIVE_TILE_LAUNCH();
    launch_pmwt_refinement_and_rbint(grid,
                                     block,
                                     d.cfa,
                                     d.rbm,
                                     d.rbp,
                                     d.pmwt,
                                     d.pmwtalt,
                                     d.rbint,
                                     rr1,
                                     cc1,
                                     stream,
                                     !fastLaunchChecks);
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_diagonal_green_correction", grid, block, stream,
                   k_diagonal_green_correction<<<grid, block, 0, stream>>>(d.cfa,
                                                                           d.dirwts0,
                                                                           d.dirwts1,
                                                                           d.hvwt,
                                                                           d.pmwt,
                                                                           d.rbint,
                                                                           d.dgrb0,
                                                                           d.rgbgreen,
                                                                           rr1,
                                                                           cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_chrominance_coset_split", grid, block, stream,
                   k_chrominance_coset_split<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_fancy_chrominance_interpolation", grid, block, stream,
                   k_fancy_chrominance_interpolation<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1));
    CHECK_LIVE_TILE_LAUNCH();
    IGPU_LT_LAUNCH("k_final_output_planes", grid, block, stream,
                   k_final_output_planes<<<grid, block, 0, stream>>>(d.rgbgreen,
                                                                     d.hvwt,
                                                                     d.dgrb0,
                                                                     d.dgrb1,
                                                                     d.red,
                                                                     d.green,
                                                                     d.blue,
                                                                     rr1,
                                                                     cc1));
    CHECK_LIVE_TILE_LAUNCH();
    if (dRgba16)
    {
        IGPU_LT_LAUNCH("k_store_tile_rgba16_post_wb_undo", grid, block, stream,
                       k_store_tile_rgba16_post_wb_undo<<<grid, block, 0, stream>>>(
                           d.red,
                           d.green,
                           d.blue,
                           dRgba16,
                           width,
                           height,
                           top,
                           left,
                           rr1,
                           cc1,
                           blackLevel,
                           wbMultiplierR,
                           wbMultiplierG,
                           wbMultiplierB));
    }
    else
    {
        IGPU_LT_LAUNCH("k_store_tile_rgb16", grid, block, stream,
                       k_store_tile_rgb16<<<grid, block, 0, stream>>>(d.red,
                                                                      d.green,
                                                                      d.blue,
                                                                      dRgb16,
                                                                      width,
                                                                      height,
                                                                      top,
                                                                      left,
                                                                      rr1,
                                                                      cc1));
    }
    CK(cudaGetLastError());
#undef CHECK_LIVE_TILE_LAUNCH
}

void clear_nyquist_dependent_scratch(const DeviceBuffers & d,
                                     cudaStream_t stream)
{
    CK(cudaMemsetAsync(d.nyquist,
                       0,
                       kHalfTileSamples * sizeof(unsigned char),
                       stream));
    CK(cudaMemsetAsync(d.dgrb2h,
                       0,
                       kHalfTileSamples * sizeof(float),
                       stream));
    CK(cudaMemsetAsync(d.dgrb2v,
                       0,
                       kHalfTileSamples * sizeof(float),
                       stream));
}

void run_frame_to_device_rgb16(const float * dRaw,
                               uint16_t * dRgb16,
                               DeviceBuffers & d,
                               int width,
                               int height)
{
    for (int top = -16; top < height; top += kTileSize - 32)
    {
        for (int left = -16; left < width; left += kTileSize - 32)
        {
            run_tile(dRaw, dRgb16, d, width, height, top, left);
        }
    }
}

void run_frame_to_device_rgb16_bayer16_post_wb_batched(
    const uint16_t * dBayer16,
    uint16_t * dRgb16,
    uint16_t * dRgba16,
    std::vector<DeviceBuffers> & buffers,
    const std::vector<cudaStream_t> & streams,
    int width,
    int height,
    int blackLevel,
    double wbMultiplierR,
    double wbMultiplierG,
    double wbMultiplierB,
    bool fastLaunchChecks = false,
    LiveRunProbe * probe = nullptr)
{
    const std::size_t batchSize = std::min(buffers.size(), streams.size());
    if (batchSize == 0)
    {
        return;
    }

    /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0 (armed runs only): start on stream 0 before the first
     * tile; after the launch loop every active stream is joined into stream 0, then end. */
    if (probe && probe->ok)
    {
        const cudaError_t rc = cudaEventRecord(probe->start, streams[0]);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "probe:event_start");
            probe->ok = false;
        }
    }

    std::size_t inFlight = 0;
    for (int top = -16; top < height; top += kTileSize - 32)
    {
        for (int left = -16; left < width; left += kTileSize - 32)
        {
            const std::size_t slot = inFlight % batchSize;
            if (inFlight >= batchSize)
            {
                CK(IGPU_LT_SYNC_CALL("tiles:slot_reuse_sync", cudaStreamSynchronize(streams[slot])));
            }
            run_tile_bayer16_post_wb(dBayer16,
                                     dRgb16,
                                     dRgba16,
                                     buffers[slot],
                                     width,
                                     height,
                                     top,
                                     left,
                                     blackLevel,
                                     wbMultiplierR,
                                     wbMultiplierG,
                                     wbMultiplierB,
                                     streams[slot],
                                     fastLaunchChecks);
            ++inFlight;
        }
    }

    const std::size_t active = std::min(inFlight, batchSize);
    if (probe && probe->ok)
    {
        cudaError_t rc = cudaSuccess;
        for (std::size_t slot = 0; slot < active && rc == cudaSuccess; ++slot)
        {
            rc = cudaEventRecord(probe->mark, streams[slot]);
            if (rc == cudaSuccess) rc = cudaStreamWaitEvent(streams[0], probe->mark, 0);
        }
        if (rc == cudaSuccess) rc = cudaEventRecord(probe->end, streams[0]);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "probe:event_join");
            probe->ok = false;
        }
    }
    for (std::size_t slot = 0; slot < active; ++slot)
    {
        CK(IGPU_LT_SYNC_CALL("tiles:drain_sync", cudaStreamSynchronize(streams[slot])));
    }
}

void run_frame_to_device_rgb16_batched(const float * dRaw,
                                       uint16_t * dRgb16,
                                       std::vector<DeviceBuffers> & buffers,
                                       const std::vector<cudaStream_t> & streams,
                                       int width,
                                       int height)
{
    const std::size_t batchSize = std::min(buffers.size(), streams.size());
    if (batchSize == 0)
    {
        return;
    }

    std::size_t inFlight = 0;
    for (int top = -16; top < height; top += kTileSize - 32)
    {
        for (int left = -16; left < width; left += kTileSize - 32)
        {
            const std::size_t slot = inFlight % batchSize;
            if (inFlight >= batchSize)
            {
                CK(IGPU_LT_SYNC_CALL("tiles:slot_reuse_sync", cudaStreamSynchronize(streams[slot])));
            }
            run_tile(dRaw,
                     dRgb16,
                     buffers[slot],
                     width,
                     height,
                     top,
                     left,
                     streams[slot]);
            ++inFlight;
        }
    }

    const std::size_t active = std::min(inFlight, batchSize);
    for (std::size_t slot = 0; slot < active; ++slot)
    {
        CK(IGPU_LT_SYNC_CALL("tiles:drain_sync", cudaStreamSynchronize(streams[slot])));
    }
}

int copy_rgb16_to_gl_rgba16_texture(const uint16_t * dRgb16,
                                    int width,
                                    int height,
                                    unsigned int glTexture)
{
    if (!dRgb16 || width <= 0 || height <= 0 || glTexture == 0) return -1;

    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const std::size_t rgbaBytes = pixelCount * 4u * sizeof(uint16_t);
    uint16_t * dRgba16 = nullptr;
    cudaGraphicsResource * resource = nullptr;

    try
    {
        CK(cudaMalloc(&dRgba16, rgbaBytes));
        const int threads = 256;
        const int blocks = static_cast<int>((pixelCount + threads - 1u) / threads);
        IGPU_LT_LAUNCH("k_pack_rgb16_to_rgba16", blocks, threads, 0,
                       k_pack_rgb16_to_rgba16<<<blocks, threads>>>(dRgb16, dRgba16, pixelCount));
        CK(cudaGetLastError());

        cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                     glTexture,
                                                     GL_TEXTURE_2D,
                                                     cudaGraphicsRegisterFlagsWriteDiscard);
        if (rc != cudaSuccess)
        {
            /* r8 class sweep: the scratch free's code is noted too, at its own site. */
            const cudaError_t freeRc = cudaFree(dRgba16);
            const igpu_cuda_error_slot::Evaluated registerFailure[] = {
                {(int)rc, "gl_rgba16:register"},
                {(int)freeRc, "gl_rgba16:free"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(registerFailure);
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(rc));
            return 2;
        }

        rc = IGPU_LT_SYNC_CALL("gl_rgba16:map", cudaGraphicsMapResources(1, &resource, 0));
        if (rc != cudaSuccess)
        {
            /* r8 (sol r7 blocker): the unregister and free codes are noted too, each at its own
             * site, before the return. */
            const cudaError_t unregisterRc = cudaGraphicsUnregisterResource(resource);
            const cudaError_t freeRc = cudaFree(dRgba16);
            const igpu_cuda_error_slot::Evaluated mapFailure[] = {
                {(int)rc, "gl_rgba16:map"},
                {(int)unregisterRc, "gl_rgba16:unregister"},
                {(int)freeRc, "gl_rgba16:free"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(mapFailure);
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(rc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        /* r8 class sweep: the mapped-array code keeps its own site (it was noted as the copy's). */
        const cudaError_t arrayRc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (arrayRc == cudaSuccess)
        {
            const std::size_t rowBytes = static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = IGPU_LT_SYNC_CALL("gl_rgba16:copy_to_array",
                                   cudaMemcpy2DToArray(mappedArray,
                                                       0,
                                                       0,
                                                       dRgba16,
                                                       rowBytes,
                                                       rowBytes,
                                                       static_cast<std::size_t>(height),
                                                       cudaMemcpyDeviceToDevice));
        }

        const cudaError_t unmapRc =
            IGPU_LT_SYNC_CALL("gl_rgba16:unmap", cudaGraphicsUnmapResources(1, &resource, 0));
        const cudaError_t unregisterRc = cudaGraphicsUnregisterResource(resource);
        const cudaError_t freeRc = cudaFree(dRgba16);
        if (arrayRc != cudaSuccess || rc != cudaSuccess || unmapRc != cudaSuccess
            || unregisterRc != cudaSuccess || freeRc != cudaSuccess)
        {
            /* r7: all evaluated codes are noted, each at its own site, before the return. */
            const igpu_cuda_error_slot::Evaluated evaluated[] = {
                {(int)arrayRc, "gl_rgba16:mapped_array"},
                {(int)rc, "gl_rgba16:copy_to_array"},
                {(int)unmapRc, "gl_rgba16:unmap"},
                {(int)unregisterRc, "gl_rgba16:unregister"},
                {(int)freeRc, "gl_rgba16:free"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(evaluated);
            if (arrayRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsSubResourceGetMappedArray(GL_RGBA16 texture) failed: %s\n",
                             cudaGetErrorString(arrayRc));
            if (rc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] device-to-GL texture copy failed: %s\n",
                             cudaGetErrorString(rc));
            if (unmapRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 texture) failed: %s\n",
                             cudaGetErrorString(unmapRc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            return 2;
        }
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        /* r8 class sweep: the cleanup codes are noted too (check_cuda noted the throw's). */
        const cudaError_t catchUnregisterRc =
            resource ? cudaGraphicsUnregisterResource(resource) : cudaSuccess;
        const cudaError_t catchFreeRc = cudaFree(dRgba16);
        if (catchUnregisterRc != cudaSuccess || catchFreeRc != cudaSuccess)
        {
            const igpu_cuda_error_slot::Evaluated catchCleanup[] = {
                {(int)catchUnregisterRc, "gl_rgba16:unregister"},
                {(int)catchFreeRc, "gl_rgba16:free"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(catchCleanup);
        }
        return -2;
    }

    return 0;
}

int copy_rgba16_to_gl_rgba16_texture(const uint16_t * dRgba16,
                                     int width,
                                     int height,
                                     unsigned int glTexture,
                                     CachedGlImageResource * cache = nullptr)
{
    if (!dRgba16 || width <= 0 || height <= 0 || glTexture == 0) return -1;

    cudaGraphicsResource * resource = nullptr;
    const bool cached = cache != nullptr;

    try
    {
        if (cached)
        {
            const int cacheRc = ensure_cached_gl_resource(cache,
                                                          glTexture,
                                                          width,
                                                          height,
                                                          cudaGraphicsRegisterFlagsWriteDiscard,
                                                          "cached GL_RGBA16 direct texture");
            if (cacheRc != 0)
            {
                return cacheRc;
            }
            resource = cache->resource;
        }
        else
        {
            cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                         glTexture,
                                                         GL_TEXTURE_2D,
                                                         cudaGraphicsRegisterFlagsWriteDiscard);
            if (rc != cudaSuccess)
            {
                IGPU_CUDA_ERROR_NOTE(rc, "gl_rgba16_direct:register");
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(rc));
                return 2;
            }
        }

        cudaError_t rc = IGPU_LT_SYNC_CALL("gl_rgba16_direct:map", cudaGraphicsMapResources(1, &resource, 0));
        if (rc != cudaSuccess)
        {
            /* r8 (sol r7 blocker): an uncached resource's unregister code is noted too, at its
             * own site, before the return (the cached reset notes its own). */
            const cudaError_t unregisterRc =
                cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
            const igpu_cuda_error_slot::Evaluated mapFailure[] = {
                {(int)rc, "gl_rgba16_direct:map"},
                {(int)unregisterRc, "gl_rgba16_direct:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(mapFailure);
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 direct texture) failed: %s\n",
                         cudaGetErrorString(rc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        /* r8 class sweep: the mapped-array code keeps its own site (it was noted as the copy's). */
        const cudaError_t arrayRc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (arrayRc == cudaSuccess)
        {
            const std::size_t rowBytes =
                static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = IGPU_LT_SYNC_CALL("gl_rgba16_direct:copy_to_array",
                                   cudaMemcpy2DToArray(mappedArray,
                                                       0,
                                                       0,
                                                       dRgba16,
                                                       rowBytes,
                                                       rowBytes,
                                                       static_cast<std::size_t>(height),
                                                       cudaMemcpyDeviceToDevice));
        }

        const cudaError_t unmapRc =
            IGPU_LT_SYNC_CALL("gl_rgba16_direct:unmap", cudaGraphicsUnmapResources(1, &resource, 0));
        const cudaError_t unregisterRc =
            cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
        if (arrayRc != cudaSuccess || rc != cudaSuccess || unmapRc != cudaSuccess || unregisterRc != cudaSuccess)
        {
            /* r7: all evaluated codes are noted, each at its own site, before the return. */
            const igpu_cuda_error_slot::Evaluated evaluated[] = {
                {(int)arrayRc, "gl_rgba16_direct:mapped_array"},
                {(int)rc, "gl_rgba16_direct:copy_to_array"},
                {(int)unmapRc, "gl_rgba16_direct:unmap"},
                {(int)unregisterRc, "gl_rgba16_direct:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(evaluated);
            if (arrayRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsSubResourceGetMappedArray(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(arrayRc));
            if (rc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] direct RGBA16 device-to-GL texture copy failed: %s\n",
                             cudaGetErrorString(rc));
            if (unmapRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(unmapRc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            /* unregisterRc fails only uncached, so this resets as before. */
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        return 0;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        if (cached) reset_cached_gl_resource(cache, false);
        /* r8 class sweep: an uncached resource's unregister code is noted too. */
        const cudaError_t catchUnregisterRc =
            (!cached && resource) ? cudaGraphicsUnregisterResource(resource) : cudaSuccess;
        if (catchUnregisterRc != cudaSuccess)
        {
            const igpu_cuda_error_slot::Evaluated catchCleanup[] = {
                {(int)catchUnregisterRc, "gl_rgba16_direct:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(catchCleanup);
        }
        return 2;
    }
}
} // namespace

extern "C" igpu_amaze_debayer_backend * igpu_amaze_debayer_create(const char * backend_name)
{
    if (backend_name && std::strcmp(backend_name, "cuda") != 0) return nullptr;

    try
    {
        int deviceCount = 0;
        CK(cudaGetDeviceCount(&deviceCount));
        if (deviceCount < 1) return nullptr;
        CK(cudaSetDevice(0));

        cudaDeviceProp prop;
        CK(cudaGetDeviceProperties(&prop, 0));

        igpu_amaze_debayer_backend * backend = new igpu_amaze_debayer_backend();
        backend->description = std::string("CUDA AMaZE / ") + prop.name;
        std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));
        igpu_lt::attach();
        return backend;
    }
    catch (const CudaStageProbeError &)
    {
        return nullptr;
    }
}

int copy_rgb16_to_gl_rgba16_texture_post_wb_undo(const uint16_t * dRgb16,
                                                 int width,
                                                 int height,
                                                 unsigned int glTexture,
                                                 int blackLevel,
                                                 double wbMultiplierR,
                                                 double wbMultiplierG,
                                                 double wbMultiplierB,
                                                 uint16_t * dRgba16Scratch = nullptr,
                                                 CachedGlImageResource * cache = nullptr)
{
    if (!dRgb16 || width <= 0 || height <= 0 || glTexture == 0) return -1;
    if (wbMultiplierR <= 0.0 || wbMultiplierG <= 0.0 || wbMultiplierB <= 0.0) return -1;

    if (blackLevel < 1000) blackLevel = -1000;

    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const std::size_t rgbaBytes = pixelCount * 4u * sizeof(uint16_t);
    uint16_t * dRgba16 = dRgba16Scratch;
    const bool ownsScratch = dRgba16 == nullptr;
    cudaGraphicsResource * resource = nullptr;
    const bool cached = cache != nullptr;
    auto releaseScratch = [&]()
    {
        if (ownsScratch && dRgba16)
        {
            /* r8 class sweep: the free's code is noted at its own site (every caller). */
            const cudaError_t freeRc = cudaFree(dRgba16);
            dRgba16 = nullptr;
            if (freeRc != cudaSuccess)
            {
                IGPU_CUDA_ERROR_NOTE(freeRc, "gl_rgba16_post_wb:free");
            }
        }
    };

    try
    {
        if (ownsScratch) CK(cudaMalloc(&dRgba16, rgbaBytes));
        const int threads = 256;
        const int blocks = static_cast<int>((pixelCount + threads - 1u) / threads);
        IGPU_LT_LAUNCH("k_pack_rgb16_to_rgba16_post_wb_undo", blocks, threads, 0,
                       k_pack_rgb16_to_rgba16_post_wb_undo<<<blocks, threads>>>(dRgb16,
                                                                                dRgba16,
                                                                                pixelCount,
                                                                                blackLevel,
                                                                                wbMultiplierR,
                                                                                wbMultiplierG,
                                                                                wbMultiplierB));
        CK(cudaGetLastError());

        if (cached)
        {
            const int cacheRc = ensure_cached_gl_resource(cache,
                                                          glTexture,
                                                          width,
                                                          height,
                                                          cudaGraphicsRegisterFlagsWriteDiscard,
                                                          "cached GL_RGBA16 post-WB texture");
            if (cacheRc != 0)
            {
                releaseScratch();
                return cacheRc;
            }
            resource = cache->resource;
        }
        else
        {
            cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                         glTexture,
                                                         GL_TEXTURE_2D,
                                                         cudaGraphicsRegisterFlagsWriteDiscard);
            if (rc != cudaSuccess)
            {
                IGPU_CUDA_ERROR_NOTE(rc, "gl_rgba16_post_wb:register");
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(rc));
                releaseScratch();
                return 2;
            }
        }

        cudaError_t rc = IGPU_LT_SYNC_CALL("gl_rgba16_post_wb:map", cudaGraphicsMapResources(1, &resource, 0));
        if (rc != cudaSuccess)
        {
            /* r8 (sol r7 blocker): an uncached resource's unregister code is noted too, at its
             * own site, before the return (the cached reset notes its own). */
            const cudaError_t unregisterRc =
                cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
            const igpu_cuda_error_slot::Evaluated mapFailure[] = {
                {(int)rc, "gl_rgba16_post_wb:map"},
                {(int)unregisterRc, "gl_rgba16_post_wb:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(mapFailure);
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 post-WB texture) failed: %s\n",
                         cudaGetErrorString(rc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            if (cached) reset_cached_gl_resource(cache, false);
            releaseScratch();
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        /* r8 class sweep: the mapped-array code keeps its own site (it was noted as the copy's). */
        const cudaError_t arrayRc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (arrayRc == cudaSuccess)
        {
            const std::size_t rowBytes = static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = IGPU_LT_SYNC_CALL("gl_rgba16_post_wb:copy_to_array",
                                   cudaMemcpy2DToArray(mappedArray,
                                                       0,
                                                       0,
                                                       dRgba16,
                                                       rowBytes,
                                                       rowBytes,
                                                       static_cast<std::size_t>(height),
                                                       cudaMemcpyDeviceToDevice));
        }

        const cudaError_t unmapRc =
            IGPU_LT_SYNC_CALL("gl_rgba16_post_wb:unmap", cudaGraphicsUnmapResources(1, &resource, 0));
        const cudaError_t unregisterRc =
            cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
        if (arrayRc != cudaSuccess || rc != cudaSuccess || unmapRc != cudaSuccess || unregisterRc != cudaSuccess)
        {
            /* r7: all evaluated codes are noted, each at its own site, before the return. */
            const igpu_cuda_error_slot::Evaluated evaluated[] = {
                {(int)arrayRc, "gl_rgba16_post_wb:mapped_array"},
                {(int)rc, "gl_rgba16_post_wb:copy_to_array"},
                {(int)unmapRc, "gl_rgba16_post_wb:unmap"},
                {(int)unregisterRc, "gl_rgba16_post_wb:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(evaluated);
            /* r8: the scratch free (it notes its own code) now follows these notes. */
            releaseScratch();
            if (arrayRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsSubResourceGetMappedArray(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(arrayRc));
            if (rc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] post-WB device-to-GL texture copy failed: %s\n",
                             cudaGetErrorString(rc));
            if (unmapRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(unmapRc));
            if (unregisterRc != cudaSuccess)
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(unregisterRc));
            /* unregisterRc fails only uncached, so this resets as before. */
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        releaseScratch();
        return 0;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        if (cached) reset_cached_gl_resource(cache, false);
        /* r8 class sweep: an uncached resource's unregister code is noted too. */
        const cudaError_t catchUnregisterRc =
            (!cached && resource) ? cudaGraphicsUnregisterResource(resource) : cudaSuccess;
        if (catchUnregisterRc != cudaSuccess)
        {
            const igpu_cuda_error_slot::Evaluated catchCleanup[] = {
                {(int)catchUnregisterRc, "gl_rgba16_post_wb:unregister"},
            };
            IGPU_CUDA_ERROR_NOTE_EVALUATED(catchCleanup);
        }
        releaseScratch();
        return 2;
    }
}

extern "C" void igpu_amaze_debayer_destroy(igpu_amaze_debayer_backend * backend)
{
    if (backend) free_probe_and_kick(backend);
    free_live_texture_buffers(backend);
    delete backend;
    igpu_lt::teardown("destroy");
}

extern "C" int igpu_amaze_debayer_abi_version(igpu_amaze_debayer_backend *)
{
    return IGPU_AMAZE_DEBAYER_ABI_VERSION;
}

extern "C" const char * igpu_amaze_debayer_describe(igpu_amaze_debayer_backend * backend)
{
    return backend ? backend->description.c_str() : "unavailable";
}

extern "C" int igpu_amaze_debayer_run(igpu_amaze_debayer_backend * backend,
                                      const float * in_raw_float,
                                      uint16_t * out_rgb16,
                                      int width,
                                      int height)
{
    if (!backend || !in_raw_float || !out_rgb16 || width <= 32 || height <= 32)
    {
        return -1;
    }

    float * dRaw = nullptr;
    uint16_t * dRgb16 = nullptr;
    uint16_t * dRgb16Hnyquist = nullptr;
    DeviceBuffers d;
    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const std::size_t rawBytes = pixelCount * sizeof(float);
    const std::size_t rgbBytes = pixelCount * 3u * sizeof(uint16_t);
    std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));

    const double totalStart = now_ms();
    igpu_lt::FrameScope lt_frame(width, height);
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(IGPU_LT_SYNC_CALL("offline:upload_h2d", cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice)));
        CK(IGPU_LT_SYNC_CALL("offline:upload_sync", cudaDeviceSynchronize()));
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(IGPU_LT_SYNC_CALL("offline:kernel_sync", cudaDeviceSynchronize()));
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double downloadStart = now_ms();
        CK(IGPU_LT_SYNC_CALL("offline:download_d2h", cudaMemcpy(out_rgb16, dRgb16, rgbBytes, cudaMemcpyDeviceToHost)));
        CK(IGPU_LT_SYNC_CALL("offline:download_sync", cudaDeviceSynchronize()));
        backend->lastTiming.download_ms = now_ms() - downloadStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;
        igpu_lt::poll();

        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        return 0;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        backend->lastTiming.total_ms = now_ms() - totalStart;
        return -2;
    }
}

extern "C" int igpu_amaze_debayer_run_gl_texture(igpu_amaze_debayer_backend * backend,
                                                 const float * in_raw_float,
                                                 unsigned int gl_texture,
                                                 int width,
                                                 int height)
{
    if (!backend || !in_raw_float || gl_texture == 0 || width <= 32 || height <= 32)
    {
        return -1;
    }

    float * dRaw = nullptr;
    uint16_t * dRgb16 = nullptr;
    uint16_t * dRgb16Hnyquist = nullptr;
    DeviceBuffers d;
    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const std::size_t rawBytes = pixelCount * sizeof(float);
    const std::size_t rgbBytes = pixelCount * 3u * sizeof(uint16_t);
    std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));

    const double totalStart = now_ms();
    igpu_lt::FrameScope lt_frame(width, height);
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(IGPU_LT_SYNC_CALL("offline:upload_h2d", cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice)));
        CK(IGPU_LT_SYNC_CALL("offline:upload_sync", cudaDeviceSynchronize()));
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(IGPU_LT_SYNC_CALL("offline:kernel_sync", cudaDeviceSynchronize()));
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double handoffStart = now_ms();
        const int rc = copy_rgb16_to_gl_rgba16_texture(dRgb16, width, height, gl_texture);
        CK(IGPU_LT_SYNC_CALL("offline:handoff_sync", cudaDeviceSynchronize()));
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;
        igpu_lt::poll();

        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        return rc;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        backend->lastTiming.total_ms = now_ms() - totalStart;
        return -2;
    }
}

extern "C" int igpu_amaze_debayer_run_post_wb_gl_texture(igpu_amaze_debayer_backend * backend,
                                                         const float * in_raw_float,
                                                         unsigned int gl_texture,
                                                         int width,
                                                         int height,
                                                         int black_level,
                                                         double wb_multiplier_r,
                                                         double wb_multiplier_g,
                                                         double wb_multiplier_b)
{
    if (!backend || !in_raw_float || gl_texture == 0 || width <= 32 || height <= 32
        || wb_multiplier_r <= 0.0 || wb_multiplier_g <= 0.0 || wb_multiplier_b <= 0.0)
    {
        return -1;
    }

    float * dRaw = nullptr;
    uint16_t * dRgb16 = nullptr;
    uint16_t * dRgb16Hnyquist = nullptr;
    DeviceBuffers d;
    const std::size_t pixelCount =
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    const std::size_t rawBytes = pixelCount * sizeof(float);
    const std::size_t rgbBytes = pixelCount * 3u * sizeof(uint16_t);
    std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));

    const double totalStart = now_ms();
    igpu_lt::FrameScope lt_frame(width, height);
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(IGPU_LT_SYNC_CALL("offline:upload_h2d", cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice)));
        CK(IGPU_LT_SYNC_CALL("offline:upload_sync", cudaDeviceSynchronize()));
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(IGPU_LT_SYNC_CALL("offline:kernel_sync", cudaDeviceSynchronize()));
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double handoffStart = now_ms();
        const int rc =
            copy_rgb16_to_gl_rgba16_texture_post_wb_undo(dRgb16,
                                                         width,
                                                         height,
                                                         gl_texture,
                                                         black_level,
                                                         wb_multiplier_r,
                                                         wb_multiplier_g,
                                                         wb_multiplier_b);
        CK(IGPU_LT_SYNC_CALL("offline:handoff_sync", cudaDeviceSynchronize()));
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;
        igpu_lt::poll();

        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        return rc;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        free_device(&d);
        cudaFree(dRaw);
        cudaFree(dRgb16);
        cudaFree(dRgb16Hnyquist);
        backend->lastTiming.total_ms = now_ms() - totalStart;
        return -2;
    }
}

extern "C" int igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture(
    igpu_amaze_debayer_backend * backend,
    unsigned int in_r16_gl_texture,
    unsigned int out_rgba16_gl_texture,
    int width,
    int height,
    int black_level,
    double wb_multiplier_r,
    double wb_multiplier_g,
    double wb_multiplier_b)
{
    if (!backend || in_r16_gl_texture == 0 || out_rgba16_gl_texture == 0
        || width <= 32 || height <= 32
        || wb_multiplier_r <= 0.0 || wb_multiplier_g <= 0.0 || wb_multiplier_b <= 0.0)
    {
        return -1;
    }

    if (black_level < 1000) black_level = -1000;

    std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));
    backend->lastProbe = igpu_amaze_probe_t{};

    const double totalStart = now_ms();
    igpu_lt::FrameScope lt_frame(width, height);
    try
    {
        ensure_live_texture_buffers(backend, width, height);
        const double uploadStart = now_ms();
        const int copyRc = copy_gl_r16_texture_to_bayer16(in_r16_gl_texture,
                                                          backend->liveReconBayer16,
                                                          width,
                                                          height,
                                                          &backend->liveInputR16Resource);
        if (copyRc != 0) return copyRc;
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: armed runs only, outside kernel_ms. */
        LiveRunProbe probeStorage;
        LiveRunProbe * probe = begin_live_run_probe(backend, &probeStorage);
        const double kernelStart = now_ms();
        /* r3: the reduced H-Nyquist filter works on device RGB16 before the WB-undo
         * pack, so a reduced run never stores tiles straight to RGBA16. */
        const bool hnyquist = backend->reducedHnyquist != 0;
        const bool directRgbaStore = !hnyquist && live_direct_rgba_store_from_env();
        run_frame_to_device_rgb16_bayer16_post_wb_batched(
            backend->liveReconBayer16,
            directRgbaStore ? nullptr : backend->liveRgb16,
            directRgbaStore ? backend->liveRgba16 : nullptr,
            backend->liveTileDeviceBuffers,
            backend->liveTileStreams,
            width,
            height,
            black_level,
            wb_multiplier_r,
            wb_multiplier_g,
            wb_multiplier_b,
            live_fast_launch_checks_from_env(),
            probe);
        stop_clock_kick_for_live_run(backend);
        const uint16_t * packRgb16 = backend->liveRgb16;
        if (hnyquist)
        {
            launch_reduced_hnyquist121(backend->liveRgb16,
                                       backend->liveRgb16Hnyquist,
                                       width,
                                       height);
            CK(cudaGetLastError());
            packRgb16 = backend->liveRgb16Hnyquist;
        }
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double handoffStart = now_ms();
        const int rc =
            directRgbaStore
                ? copy_rgba16_to_gl_rgba16_texture(
                    backend->liveRgba16,
                    width,
                    height,
                    out_rgba16_gl_texture,
                    &backend->liveOutputRgba16Resource)
                : copy_rgb16_to_gl_rgba16_texture_post_wb_undo(
                    packRgb16,
                    width,
                    height,
                    out_rgba16_gl_texture,
                    black_level,
                    wb_multiplier_r,
                    wb_multiplier_g,
                    wb_multiplier_b,
                    backend->liveRgba16,
                    &backend->liveOutputRgba16Resource);
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;
        /* Row 0: device_ms and mhz_after, after every timing field (armed runs only) */
        finish_live_run_probe(backend, probe);
        igpu_lt::poll();
        return rc;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        backend->lastTiming.total_ms = now_ms() - totalStart;
        return -2;
    }
}

extern "C" int igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16(
    igpu_amaze_debayer_backend * backend,
    const uint16_t * device_bayer16,
    unsigned int out_rgba16_gl_texture,
    int width,
    int height,
    int black_level,
    double wb_multiplier_r,
    double wb_multiplier_g,
    double wb_multiplier_b)
{
    if (!backend || !device_bayer16 || out_rgba16_gl_texture == 0
        || width <= 32 || height <= 32
        || wb_multiplier_r <= 0.0 || wb_multiplier_g <= 0.0 || wb_multiplier_b <= 0.0)
    {
        return -1;
    }

    if (black_level < 1000) black_level = -1000;

    std::memset(&backend->lastTiming, 0, sizeof(backend->lastTiming));
    backend->lastProbe = igpu_amaze_probe_t{};

    const double totalStart = now_ms();
    igpu_lt::FrameScope lt_frame(width, height);
    try
    {
        ensure_live_texture_buffers(backend, width, height);
        backend->lastTiming.upload_ms = 0.0;

        /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: armed runs only, outside kernel_ms. */
        LiveRunProbe probeStorage;
        LiveRunProbe * probe = begin_live_run_probe(backend, &probeStorage);
        const double kernelStart = now_ms();
        /* r3: the reduced H-Nyquist filter works on device RGB16 before the WB-undo
         * pack, so a reduced run never stores tiles straight to RGBA16. */
        const bool hnyquist = backend->reducedHnyquist != 0;
        const bool directRgbaStore = !hnyquist && live_direct_rgba_store_from_env();
        run_frame_to_device_rgb16_bayer16_post_wb_batched(
            device_bayer16,
            directRgbaStore ? nullptr : backend->liveRgb16,
            directRgbaStore ? backend->liveRgba16 : nullptr,
            backend->liveTileDeviceBuffers,
            backend->liveTileStreams,
            width,
            height,
            black_level,
            wb_multiplier_r,
            wb_multiplier_g,
            wb_multiplier_b,
            live_fast_launch_checks_from_env(),
            probe);
        stop_clock_kick_for_live_run(backend);
        const uint16_t * packRgb16 = backend->liveRgb16;
        if (hnyquist)
        {
            launch_reduced_hnyquist121(backend->liveRgb16,
                                       backend->liveRgb16Hnyquist,
                                       width,
                                       height);
            CK(cudaGetLastError());
            packRgb16 = backend->liveRgb16Hnyquist;
        }
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double handoffStart = now_ms();
        const int rc =
            directRgbaStore
                ? copy_rgba16_to_gl_rgba16_texture(
                    backend->liveRgba16,
                    width,
                    height,
                    out_rgba16_gl_texture,
                    &backend->liveOutputRgba16Resource)
                : copy_rgb16_to_gl_rgba16_texture_post_wb_undo(
                    packRgb16,
                    width,
                    height,
                    out_rgba16_gl_texture,
                    black_level,
                    wb_multiplier_r,
                    wb_multiplier_g,
                    wb_multiplier_b,
                    backend->liveRgba16,
                    &backend->liveOutputRgba16Resource);
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;
        /* Row 0: device_ms and mhz_after, after every timing field (armed runs only) */
        finish_live_run_probe(backend, probe);
        igpu_lt::poll();
        return rc;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        backend->lastTiming.total_ms = now_ms() - totalStart;
        return -2;
    }
}

extern "C" int igpu_amaze_debayer_set_reduced_hnyquist(igpu_amaze_debayer_backend * backend,
                                                       int enable)
{
    if (!backend || (enable != 0 && enable != 1)) return -1;
    backend->reducedHnyquist = enable;
    return 0;
}

extern "C" int igpu_amaze_debayer_reset_live_gl_texture_resources(igpu_amaze_debayer_backend * backend)
{
    if (!backend) return -1;
    reset_live_gl_resources(backend);
    return 0;
}

extern "C" int igpu_amaze_debayer_last_timing(igpu_amaze_debayer_backend * backend,
                                               igpu_amaze_debayer_timing_t * timing)
{
    if (!backend || !timing) return -1;
    *timing = backend->lastTiming;
    return 0;
}

/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0. */
extern "C" int igpu_amaze_debayer_arm_probe(igpu_amaze_debayer_backend * backend, int calls)
{
    if (!backend || calls < 0) return -1;
    backend->probeArmedCalls = calls;
    return 0;
}

extern "C" int igpu_amaze_debayer_last_probe(igpu_amaze_debayer_backend * backend, igpu_amaze_probe_t * out)
{
    if (!backend || !out) return -1;
    *out = backend->lastProbe;
    return 0;
}

/* Row K. */
extern "C" int igpu_amaze_debayer_clock_kick_begin(igpu_amaze_debayer_backend * backend, int max_ms)
{
    if (!backend || max_ms <= 0 || max_ms > kClockKickMaxMsCap) return -1;
    if (backend->kickLaunched && backend->kickHost && !backend->kickHost->done) return 1;
    backend->kickLaunched = false;   // a finished kick nobody collected is dropped
    if (!backend->kickStream)
    {
        int leastPriority = 0;
        int greatestPriority = 0;
        cudaError_t rc = cudaDeviceGetStreamPriorityRange(&leastPriority, &greatestPriority);
        if (rc == cudaSuccess)
            rc = cudaStreamCreateWithPriority(&backend->kickStream, cudaStreamNonBlocking, leastPriority);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "clock_kick:stream");
            backend->kickStream = nullptr;
            return -2;
        }
    }
    if (!backend->kickHost)
    {
        void * host = nullptr;
        cudaError_t rc = cudaHostAlloc(&host, sizeof(ClockKickShared), cudaHostAllocMapped);
        void * device = nullptr;
        if (rc == cudaSuccess) rc = cudaHostGetDevicePointer(&device, host, 0);
        if (rc != cudaSuccess)
        {
            IGPU_CUDA_ERROR_NOTE(rc, "clock_kick:host_alloc");
            if (host) cudaFreeHost(host);
            return -2;
        }
        backend->kickHost = static_cast<ClockKickShared *>(host);
        backend->kickDevice = static_cast<ClockKickShared *>(device);
    }
    /* the previous kick's feeder has published done (or never started): joining it does not wait on the GPU */
    if (backend->kickFeeder.joinable()) backend->kickFeeder.join();
    std::memset(static_cast<void *>(backend->kickHost), 0, sizeof(ClockKickShared));
    const unsigned long long maxNs = static_cast<unsigned long long>(max_ms) * 1000000ull;
    /* the chunks are submitted by the feeder, at most kClockKickChunksInFlight of kClockKickChunkMs at a time */
    try
    {
        backend->kickFeeder = std::thread(clock_kick_feeder, backend, maxNs);
    }
    catch (...)
    {
        return -2;
    }
    backend->kickLaunched = true;
    return 0;
}

extern "C" int igpu_amaze_debayer_clock_kick_end(igpu_amaze_debayer_backend * backend,
                                                 igpu_clock_kick_report_t * out)
{
    if (!backend || !out) return -1;
    std::memset(out, 0, sizeof(*out));
    if (!backend->kickLaunched || !backend->kickHost)
    {
        out->state = 2;
        return 2;
    }
    ClockKickShared * s = backend->kickHost;
    if (s->stop == 0) s->stop = 1;
    /* host memory only, no CUDA call: bounded, and safe after a device loss */
    const double waitStart = now_ms();
    while (!s->done && now_ms() - waitStart < kClockKickEndWaitMs)
    {
    }
    if (!s->done)
    {
        out->state = 1;
        return 1;
    }
    std::atomic_thread_fence(std::memory_order_acquire);
    unsigned int count = s->count;
    if (count > IGPU_CLOCK_KICK_MAX_SAMPLES) count = IGPU_CLOCK_KICK_MAX_SAMPLES;
    for (unsigned int i = 0; i < count; ++i)
    {
        out->sample_ns[i] = s->sampleNs[i];
        out->sample_clk[i] = s->sampleClk[i];
        out->sample_sm[i] = s->sampleSm[i];
    }
    out->samples = static_cast<int>(count);
    out->stopped_by = static_cast<int>(s->exitReason);
    const unsigned long long startNs = s->startNs;
    const unsigned long long endNs = s->endNs;
    out->ran_ms = endNs > startNs ? static_cast<double>(endNs - startNs) / 1000000.0 : 0.0;
    out->state = 0;
    backend->kickLaunched = false;
    return 0;
}

extern "C" int igpu_amaze_debayer_last_cuda_error(igpu_cuda_error_t * out)
{
    return igpu_cuda_error_slot::copy_last(out);
}

/* GPU-FIELD-TELEMETRY-1 r6: every code a failed call noted, and a lock-free
 * "anything pending" word (igpu_cuda_error.h). */
extern "C" int igpu_amaze_debayer_drain_cuda_errors(int channel, igpu_cuda_error_t * out, int capacity,
                                                    unsigned long long * dropped)
{
    return igpu_cuda_error_slot::drain(channel, out, capacity, dropped);
}

extern "C" const volatile long * igpu_amaze_debayer_cuda_error_pending_word(int channel)
{
    return igpu_cuda_error_slot::pending_word(channel);
}
