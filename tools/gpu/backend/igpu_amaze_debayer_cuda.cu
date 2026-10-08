/* igpu_amaze_debayer_cuda.cu - CUDA backend for the generic AMaZE debayer C ABI.
 *
 * The AMaZE math kernels are reused from the reviewed full-stage probe:
 * tools/gpu/cuda_amaze_debayer_stage_probe.cu. This file adds the production
 * ABI wrapper plus full-frame tile orchestration; it does not change GUI/UX
 * claims by itself.
 */

#include "igpu_amaze_debayer.h"

#define CUDA_AMAZE_DEBAYER_STAGE_PROBE_LIBRARY 1
#define CUDA_AMAZE_DEBAYER_STAGE_PROBE_NO_MAIN 1
#include "../cuda_amaze_debayer_stage_probe.cu"

#include <chrono>
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

struct igpu_amaze_debayer_backend
{
    std::string description;
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

int reset_cached_gl_resource(CachedGlImageResource * cache, bool tolerateInvalidGraphicsContext)
{
    int status = 0;
    if (!cache) return status;
    if (cache->resource)
    {
        const cudaError_t rc = cudaGraphicsUnregisterResource(cache->resource);
        if (rc != cudaSuccess)
        {
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
    k_reduced_hnyquist121<<<grid, block>>>(in, out, w, h);
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
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_R16 source texture) failed: %s\n",
                         cudaGetErrorString(rc));
            return 2;
        }
    }

    cudaError_t rc = cudaGraphicsMapResources(1, &resource, 0);
    if (rc != cudaSuccess)
    {
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_R16 source texture) failed: %s\n",
                     cudaGetErrorString(rc));
        if (cached) reset_cached_gl_resource(cache, false);
        else cudaGraphicsUnregisterResource(resource);
        return 2;
    }

    cudaArray_t mappedArray = nullptr;
    rc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
    if (rc == cudaSuccess)
    {
        const std::size_t rowBytes = static_cast<std::size_t>(width) * sizeof(uint16_t);
        rc = cudaMemcpy2DFromArray(dBayer16,
                                   rowBytes,
                                   mappedArray,
                                   0,
                                   0,
                                   rowBytes,
                                   static_cast<std::size_t>(height),
                                   cudaMemcpyDeviceToDevice);
    }

    const cudaError_t unmapRc = cudaGraphicsUnmapResources(1, &resource, 0);
    const cudaError_t unregisterRc =
        cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
    if (rc != cudaSuccess)
    {
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] GL_R16 source texture device copy failed: %s\n",
                     cudaGetErrorString(rc));
        if (cached) reset_cached_gl_resource(cache, false);
        return 2;
    }
    if (unmapRc != cudaSuccess)
    {
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_R16 source texture) failed: %s\n",
                     cudaGetErrorString(unmapRc));
        if (cached) reset_cached_gl_resource(cache, false);
        return 2;
    }
    if (unregisterRc != cudaSuccess)
    {
        std::fprintf(stderr,
                     "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_R16 source texture) failed: %s\n",
                     cudaGetErrorString(unregisterRc));
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

    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);

    k_tile_load_float<<<grid, block, 0, stream>>>(dRaw,
                                                  d.cfa,
                                                  d.rgbgreen,
                                                  width,
                                                  height,
                                                  top,
                                                  left,
                                                  rr1,
                                                  cc1);
    CK(cudaGetLastError());
    k_gradients<<<grid, block, 0, stream>>>(d.cfa, d.dirwts0, d.dirwts1, d.delhvsqsum, rr1, cc1);
    CK(cudaGetLastError());
    k_diagonal_precursors<<<grid, block, 0, stream>>>(d.cfa, d.delp, d.delm, d.dgrbsq1p, d.dgrbsq1m, rr1, cc1);
    CK(cudaGetLastError());
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
                                                      cc1);
    CK(cudaGetLastError());
    launch_variance_selection_prefix(d, rr1, cc1, stream);
    k_hvwt_adaptive_weights<<<grid, block, 0, stream>>>(d.dirwts0,
                                                        d.dirwts1,
                                                        d.vcd,
                                                        d.hcd,
                                                        d.dgintv,
                                                        d.dginth,
                                                        d.hvwt,
                                                        rr1,
                                                        cc1);
    CK(cudaGetLastError());
    clear_nyquist_dependent_scratch(d, stream);
    k_nyquist_test<<<grid, block, 0, stream>>>(d.cddiffsq, d.delhvsqsum, d.nyquist, rr1, cc1);
    CK(cudaGetLastError());
    k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads, 0, stream>>>(d.nyquist, rr1, cc1);
    CK(cudaGetLastError());
    k_nyquist_area_interpolation<<<grid, block, 0, stream>>>(d.cfa, d.nyquist, d.hvwt, rr1, cc1);
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
    k_nyquist_green_refinement<<<grid, block, 0, stream>>>(d.cfa,
                                                           d.vcd,
                                                           d.hcd,
                                                           d.nyquist,
                                                           d.dgrb2h,
                                                           d.dgrb2v,
                                                           d.dgrb0,
                                                           d.rgbgreen,
                                                           rr1,
                                                           cc1);
    CK(cudaGetLastError());
    k_diagonal_rb_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                            d.delp,
                                                            d.delm,
                                                            d.dgrbsq1p,
                                                            d.dgrbsq1m,
                                                            d.rbm,
                                                            d.rbp,
                                                            d.pmwt,
                                                            rr1,
                                                            cc1);
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
    k_diagonal_green_correction<<<grid, block, 0, stream>>>(d.cfa,
                                                            d.dirwts0,
                                                            d.dirwts1,
                                                            d.hvwt,
                                                            d.pmwt,
                                                            d.rbint,
                                                            d.dgrb0,
                                                            d.rgbgreen,
                                                            rr1,
                                                            cc1);
    CK(cudaGetLastError());
    k_chrominance_coset_split<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1);
    CK(cudaGetLastError());
    k_fancy_chrominance_interpolation<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1);
    CK(cudaGetLastError());
    k_final_output_planes<<<grid, block, 0, stream>>>(d.rgbgreen,
                                                      d.hvwt,
                                                      d.dgrb0,
                                                      d.dgrb1,
                                                      d.red,
                                                      d.green,
                                                      d.blue,
                                                      rr1,
                                                      cc1);
    CK(cudaGetLastError());
    k_store_tile_rgb16<<<grid, block, 0, stream>>>(d.red,
                                                   d.green,
                                                   d.blue,
                                                   dRgb16,
                                                   width,
                                                   height,
                                                   top,
                                                   left,
                                                   rr1,
                                                   cc1);
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

    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);

#define CHECK_LIVE_TILE_LAUNCH() \
    do { \
        if (!fastLaunchChecks) CK(cudaGetLastError()); \
    } while (0)

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
                                                            wbMultiplierB);
    CHECK_LIVE_TILE_LAUNCH();
    k_gradients<<<grid, block, 0, stream>>>(d.cfa, d.dirwts0, d.dirwts1, d.delhvsqsum, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_diagonal_precursors<<<grid, block, 0, stream>>>(d.cfa, d.delp, d.delm, d.dgrbsq1p, d.dgrbsq1m, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
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
                                                      cc1);
    CHECK_LIVE_TILE_LAUNCH();
    launch_variance_selection_prefix(d, rr1, cc1, stream, !fastLaunchChecks);
    CHECK_LIVE_TILE_LAUNCH();
    k_hvwt_adaptive_weights<<<grid, block, 0, stream>>>(d.dirwts0,
                                                        d.dirwts1,
                                                        d.vcd,
                                                        d.hcd,
                                                        d.dgintv,
                                                        d.dginth,
                                                        d.hvwt,
                                                        rr1,
                                                        cc1);
    CHECK_LIVE_TILE_LAUNCH();
    clear_nyquist_dependent_scratch(d, stream);
    k_nyquist_test<<<grid, block, 0, stream>>>(d.cddiffsq, d.delhvsqsum, d.nyquist, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads, 0, stream>>>(d.nyquist, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_nyquist_area_interpolation<<<grid, block, 0, stream>>>(d.cfa, d.nyquist, d.hvwt, rr1, cc1);
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
    k_nyquist_green_refinement<<<grid, block, 0, stream>>>(d.cfa,
                                                           d.vcd,
                                                           d.hcd,
                                                           d.nyquist,
                                                           d.dgrb2h,
                                                           d.dgrb2v,
                                                           d.dgrb0,
                                                           d.rgbgreen,
                                                           rr1,
                                                           cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_diagonal_rb_interpolation<<<grid, block, 0, stream>>>(d.cfa,
                                                            d.delp,
                                                            d.delm,
                                                            d.dgrbsq1p,
                                                            d.dgrbsq1m,
                                                            d.rbm,
                                                            d.rbp,
                                                            d.pmwt,
                                                            rr1,
                                                            cc1);
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
    k_diagonal_green_correction<<<grid, block, 0, stream>>>(d.cfa,
                                                            d.dirwts0,
                                                            d.dirwts1,
                                                            d.hvwt,
                                                            d.pmwt,
                                                            d.rbint,
                                                            d.dgrb0,
                                                            d.rgbgreen,
                                                            rr1,
                                                            cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_chrominance_coset_split<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_fancy_chrominance_interpolation<<<grid, block, 0, stream>>>(d.dgrb0, d.dgrb1, rr1, cc1);
    CHECK_LIVE_TILE_LAUNCH();
    k_final_output_planes<<<grid, block, 0, stream>>>(d.rgbgreen,
                                                      d.hvwt,
                                                      d.dgrb0,
                                                      d.dgrb1,
                                                      d.red,
                                                      d.green,
                                                      d.blue,
                                                      rr1,
                                                      cc1);
    CHECK_LIVE_TILE_LAUNCH();
    if (dRgba16)
    {
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
            wbMultiplierB);
    }
    else
    {
        k_store_tile_rgb16<<<grid, block, 0, stream>>>(d.red,
                                                       d.green,
                                                       d.blue,
                                                       dRgb16,
                                                       width,
                                                       height,
                                                       top,
                                                       left,
                                                       rr1,
                                                       cc1);
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
    bool fastLaunchChecks = false)
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
                CK(cudaStreamSynchronize(streams[slot]));
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
    for (std::size_t slot = 0; slot < active; ++slot)
    {
        CK(cudaStreamSynchronize(streams[slot]));
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
                CK(cudaStreamSynchronize(streams[slot]));
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
        CK(cudaStreamSynchronize(streams[slot]));
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
        k_pack_rgb16_to_rgba16<<<blocks, threads>>>(dRgb16, dRgba16, pixelCount);
        CK(cudaGetLastError());

        cudaError_t rc = cudaGraphicsGLRegisterImage(&resource,
                                                     glTexture,
                                                     GL_TEXTURE_2D,
                                                     cudaGraphicsRegisterFlagsWriteDiscard);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(rc));
            cudaFree(dRgba16);
            return 2;
        }

        rc = cudaGraphicsMapResources(1, &resource, 0);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(rc));
            cudaGraphicsUnregisterResource(resource);
            cudaFree(dRgba16);
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        rc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (rc == cudaSuccess)
        {
            const std::size_t rowBytes = static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = cudaMemcpy2DToArray(mappedArray,
                                     0,
                                     0,
                                     dRgba16,
                                     rowBytes,
                                     rowBytes,
                                     static_cast<std::size_t>(height),
                                     cudaMemcpyDeviceToDevice);
        }

        const cudaError_t unmapRc = cudaGraphicsUnmapResources(1, &resource, 0);
        const cudaError_t unregisterRc = cudaGraphicsUnregisterResource(resource);
        cudaFree(dRgba16);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] device-to-GL texture copy failed: %s\n",
                         cudaGetErrorString(rc));
            return 2;
        }
        if (unmapRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(unmapRc));
            return 2;
        }
        if (unregisterRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 texture) failed: %s\n",
                         cudaGetErrorString(unregisterRc));
            return 2;
        }
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        if (resource) cudaGraphicsUnregisterResource(resource);
        cudaFree(dRgba16);
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
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 direct texture) failed: %s\n",
                             cudaGetErrorString(rc));
                return 2;
            }
        }

        cudaError_t rc = cudaGraphicsMapResources(1, &resource, 0);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 direct texture) failed: %s\n",
                         cudaGetErrorString(rc));
            if (cached) reset_cached_gl_resource(cache, false);
            else cudaGraphicsUnregisterResource(resource);
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        rc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (rc == cudaSuccess)
        {
            const std::size_t rowBytes =
                static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = cudaMemcpy2DToArray(mappedArray,
                                     0,
                                     0,
                                     dRgba16,
                                     rowBytes,
                                     rowBytes,
                                     static_cast<std::size_t>(height),
                                     cudaMemcpyDeviceToDevice);
        }

        const cudaError_t unmapRc = cudaGraphicsUnmapResources(1, &resource, 0);
        const cudaError_t unregisterRc =
            cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] direct RGBA16 device-to-GL texture copy failed: %s\n",
                         cudaGetErrorString(rc));
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        if (unmapRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 direct texture) failed: %s\n",
                         cudaGetErrorString(unmapRc));
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        if (unregisterRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 direct texture) failed: %s\n",
                         cudaGetErrorString(unregisterRc));
            return 2;
        }
        return 0;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        if (cached) reset_cached_gl_resource(cache, false);
        else if (resource) cudaGraphicsUnregisterResource(resource);
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
            cudaFree(dRgba16);
            dRgba16 = nullptr;
        }
    };

    try
    {
        if (ownsScratch) CK(cudaMalloc(&dRgba16, rgbaBytes));
        const int threads = 256;
        const int blocks = static_cast<int>((pixelCount + threads - 1u) / threads);
        k_pack_rgb16_to_rgba16_post_wb_undo<<<blocks, threads>>>(dRgb16,
                                                                 dRgba16,
                                                                 pixelCount,
                                                                 blackLevel,
                                                                 wbMultiplierR,
                                                                 wbMultiplierG,
                                                                 wbMultiplierB);
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
                std::fprintf(stderr,
                             "[igpu_amaze_debayer_cuda] cudaGraphicsGLRegisterImage(GL_RGBA16 post-WB texture) failed: %s\n",
                             cudaGetErrorString(rc));
                releaseScratch();
                return 2;
            }
        }

        cudaError_t rc = cudaGraphicsMapResources(1, &resource, 0);
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsMapResources(GL_RGBA16 post-WB texture) failed: %s\n",
                         cudaGetErrorString(rc));
            if (cached) reset_cached_gl_resource(cache, false);
            else cudaGraphicsUnregisterResource(resource);
            releaseScratch();
            return 2;
        }

        cudaArray_t mappedArray = nullptr;
        rc = cudaGraphicsSubResourceGetMappedArray(&mappedArray, resource, 0, 0);
        if (rc == cudaSuccess)
        {
            const std::size_t rowBytes = static_cast<std::size_t>(width) * 4u * sizeof(uint16_t);
            rc = cudaMemcpy2DToArray(mappedArray,
                                     0,
                                     0,
                                     dRgba16,
                                     rowBytes,
                                     rowBytes,
                                     static_cast<std::size_t>(height),
                                     cudaMemcpyDeviceToDevice);
        }

        const cudaError_t unmapRc = cudaGraphicsUnmapResources(1, &resource, 0);
        const cudaError_t unregisterRc =
            cached ? cudaSuccess : cudaGraphicsUnregisterResource(resource);
        releaseScratch();
        if (rc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] post-WB device-to-GL texture copy failed: %s\n",
                         cudaGetErrorString(rc));
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        if (unmapRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnmapResources(GL_RGBA16 post-WB texture) failed: %s\n",
                         cudaGetErrorString(unmapRc));
            if (cached) reset_cached_gl_resource(cache, false);
            return 2;
        }
        if (unregisterRc != cudaSuccess)
        {
            std::fprintf(stderr,
                         "[igpu_amaze_debayer_cuda] cudaGraphicsUnregisterResource(GL_RGBA16 post-WB texture) failed: %s\n",
                         cudaGetErrorString(unregisterRc));
            return 2;
        }
        return 0;
    }
    catch (const CudaStageProbeError & error)
    {
        std::fprintf(stderr, "[igpu_amaze_debayer_cuda] %s\n", error.what());
        if (cached) reset_cached_gl_resource(cache, false);
        else if (resource) cudaGraphicsUnregisterResource(resource);
        releaseScratch();
        return 2;
    }
}

extern "C" void igpu_amaze_debayer_destroy(igpu_amaze_debayer_backend * backend)
{
    free_live_texture_buffers(backend);
    delete backend;
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
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice));
        CK(cudaDeviceSynchronize());
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(cudaDeviceSynchronize());
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double downloadStart = now_ms();
        CK(cudaMemcpy(out_rgb16, dRgb16, rgbBytes, cudaMemcpyDeviceToHost));
        CK(cudaDeviceSynchronize());
        backend->lastTiming.download_ms = now_ms() - downloadStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;

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
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice));
        CK(cudaDeviceSynchronize());
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(cudaDeviceSynchronize());
        backend->lastTiming.kernel_ms = now_ms() - kernelStart;

        const double handoffStart = now_ms();
        const int rc = copy_rgb16_to_gl_rgba16_texture(dRgb16, width, height, gl_texture);
        CK(cudaDeviceSynchronize());
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;

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
    try
    {
        CK(cudaMalloc(&dRaw, rawBytes));
        CK(cudaMalloc(&dRgb16, rgbBytes));
        if (backend->reducedHnyquist) CK(cudaMalloc(&dRgb16Hnyquist, rgbBytes));
        allocate_device(&d, 1);
        const double uploadStart = now_ms();
        CK(cudaMemcpy(dRaw, in_raw_float, rawBytes, cudaMemcpyHostToDevice));
        CK(cudaDeviceSynchronize());
        backend->lastTiming.upload_ms = now_ms() - uploadStart;

        const double kernelStart = now_ms();
        run_frame_to_device_rgb16(dRaw, dRgb16, d, width, height);
        if (backend->reducedHnyquist)
        {
            launch_reduced_hnyquist121(dRgb16, dRgb16Hnyquist, width, height);
            CK(cudaGetLastError());
            std::swap(dRgb16, dRgb16Hnyquist);
        }
        CK(cudaDeviceSynchronize());
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
        CK(cudaDeviceSynchronize());
        backend->lastTiming.download_ms = now_ms() - handoffStart;
        backend->lastTiming.total_ms = now_ms() - totalStart;

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

    const double totalStart = now_ms();
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
            live_fast_launch_checks_from_env());
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

    const double totalStart = now_ms();
    try
    {
        ensure_live_texture_buffers(backend, width, height);
        backend->lastTiming.upload_ms = 0.0;

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
            live_fast_launch_checks_from_env());
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
