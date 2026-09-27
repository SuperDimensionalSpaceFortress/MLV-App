/* igpu_recon_cuda.cu - CUDA backend implementing the igpu_recon C ABI
 * (tools/gpu/igpu_recon.h). This is the deployable form of the validated
 * parity kernel chain in tools/gpu/cuda_recon_parity.cu: the __global__
 * kernels below are copied verbatim from that file (0-LSB-correct vs the CPU
 * oracle, tools/gpu/oracle), wrapped behind the opaque backend handle and
 * exported with extern "C" __declspec(dllexport).
 *
 * WHY A DLL
 *   MLVApp is Qt/MinGW; nvcc requires MSVC. The MinGW app cannot compile CUDA,
 *   so the backend ships as an MSVC/nvcc DLL and the app loads it at runtime
 *   (LoadLibrary/GetProcAddress) through this pure-C ABI. A MinGW host calling
 *   an MSVC/nvcc DLL works precisely because the boundary is plain C (cdecl,
 *   no C++ name-mangling, POD structs).
 *
 * SCOPE / PARITY (v1)
 *   interp_method=1 (mean23), use_alias_map=0 or 1, use_fullres=1,
 *   chroma_smooth=0 or 1 (2x2 only), RGGB phase is_bright[y&3]={1,1,0,0}.
 *   The host supplies the 4 precomputed
 *   LUTs (raw2ev, ev2raw, mix_curve, fullres_curve) so no GPU libm enters the
 *   integer-LUT parity surface; build with --fmad=false for IEEE blends.
 *
 * Build (on Ultra-Magnus, via vcvars + nvcc):
 *   nvcc -arch=sm_89 --fmad=false -shared -Xcompiler "/MD"
 *        -o igpu_recon_cuda.dll igpu_recon_cuda.cu
 *   (see backend/build-backend-dll.ps1; it also emits the import .lib/.exp)
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <string>
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
#include <cuda_runtime.h>
#include <cuda_gl_interop.h>

#include "igpu_recon.h"

/* Export mechanism note:
 *   The canonical igpu_recon.h declares the ABI prototypes WITHOUT any export
 *   attribute (it is the cross-compiler contract, deliberately attribute-free,
 *   and is NOT modified here). MSVC/nvcc forbids ADDING dllexport on a later
 *   redeclaration once a plain prototype has been seen, so we cannot put
 *   __declspec(dllexport) on the definitions below after including the header.
 *   Instead the symbols are exported by name via igpu_recon_cuda.def passed to
 *   the linker (/DEF), which is the standard header-independent export path and
 *   yields the identical extern "C" exported entry points + import lib.
 *   We still mark IGPU_API on the definitions so the intent is explicit; it is
 *   defined empty here (the .def does the exporting). */
#define IGPU_API

/* ---- fixed geometry / scalars (mirrors cuda_recon_parity.cu) ----------- */
#define EV_RESOLUTION 65536
#define RAW2EV_COUNT  (1u << 20)            /* 1048576 */
#define EV2RAW_COUNT  (24u * EV_RESOLUTION) /* 1572864 */
#define EV2RAW_ORIGIN (10 * EV_RESOLUTION)  /* 655360  */
#define ALIAS_MAP_MAX 15000
#define HOST_FULLRES_THR 0.8   /* fullres_thr (dualiso.c:1678) */
#define HOST_FACTOR   0.125    /* pow(2,-3); fixed for the 3 EV / 8x ISO ratio */
#define CUDA_CONTEXT_RESERVE_BYTES (410ull * 1024ull * 1024ull)
#define RETAINED_DEVICE_OUTPUT_SLOTS 12
#define PREUPLOAD_SLOTS 4

/* Device constants are set per set_clip()/set_luts(); kept in __constant__ so
 * the verbatim kernels reference them exactly as the parity probe did. */
__device__ __constant__ int   d_black;
__device__ __constant__ int   d_white;
__device__ __constant__ int   d_white_darkened;
__device__ __constant__ int   d_dark_noise;
__device__ __constant__ double d_factor;
__device__ __constant__ int   d_black_delta20;
__device__ __constant__ int   d_is_bright[4];

#define CK(call) do { cudaError_t _e=(call); if(_e!=cudaSuccess){ \
    fprintf(stderr,"[igpu_recon_cuda] CUDA error %s @ %s:%d: %s\n",#call,__FILE__,__LINE__, \
    cudaGetErrorString(_e)); return -1;} } while(0)

/* same as CK but returns NULL (for create()) */
#define CKN(call) do { cudaError_t _e=(call); if(_e!=cudaSuccess){ \
    fprintf(stderr,"[igpu_recon_cuda] CUDA error %s @ %s:%d: %s\n",#call,__FILE__,__LINE__, \
    cudaGetErrorString(_e)); return NULL;} } while(0)

/* MIN/MAX/COERCE/ABS exactly as dualiso.c (integer + double overloads) */
__device__ __host__ static inline int   imin(int a,int b){ return a<b?a:b; }
__device__ __host__ static inline int   imax(int a,int b){ return a>b?a:b; }
__device__ __host__ static inline int   icoerce(int x,int lo,int hi){ return imax(imin(x,hi),lo); }
__device__ __host__ static inline double dmin(double a,double b){ return a<b?a:b; }
__device__ __host__ static inline double dmax(double a,double b){ return a>b?a:b; }
__device__ __host__ static inline double dcoerce(double x,double lo,double hi){ return dmax(dmin(x,hi),lo); }
__device__ __host__ static inline int   iabs_(int a){ return a>0?a:-a; }

/* ===================================================================== *
 *  VALIDATED KERNEL CHAIN (verbatim from cuda_recon_parity.cu)          *
 *  The only edit is that W/H are passed as kernel args instead of being *
 *  compile-time #defines, so the backend handles per-clip geometry.     *
 * ===================================================================== */

/* STAGE 1+2: convert_to_20bit + match_exposures apply */
__global__ void k_promote_match(const uint16_t* __restrict in, uint32_t* __restrict raw32,
                                int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i = (size_t)x + (size_t)y*W;
    int p = (int)((((uint32_t)in[i]) << 6) & 0xFFFFFu);   /* convert_to_20bit */
    if (p != 0) {                                          /* match apply */
        if (d_is_bright[y & 3]) {
            p = (int)(((double)(p - d_black + d_black_delta20)) * d_factor + (double)d_black);
        } else {
            p = (int)((double)(p - d_black_delta20) + (double)d_black_delta20 * d_factor);
        }
    }
    raw32[i] = (uint32_t)icoerce(p, 0, 0xFFFFF);          /* raw_set_pixel20 */
}

/* STAGE 3: mean23_interpolate_with_lut */
__device__ static inline int mean2_noerr(int a,int b,int white){
    if (a>=white || b>=white) return white;
    return (a+b)/2;
}
__device__ static inline int mean3_noerr(int a,int b,int c,int white){
    int m=(a+b+c)/3;
    if (a>=white||b>=white||c>=white) return imax(m,white);
    return m;
}
__global__ void k_mean23(const uint32_t* __restrict raw32,
                         uint32_t* __restrict dark, uint32_t* __restrict bright,
                         const int* __restrict raw2ev, const int* __restrict ev2raw,
                         int W, int H)
{
    int y  = blockIdx.y*blockDim.y + threadIdx.y;        /* row */
    int xi = blockIdx.x*blockDim.x + threadIdx.x;        /* index over even x's */
    if (y<2 || y>=H-2) return;
    int x = 2 + 2*xi;
    if (x >= W-3) return;

    int bright_row = d_is_bright[y & 3];
    int row_offset = y*W;
    const uint32_t* raw_row     = raw32 + row_offset;
    const uint32_t* raw_y_m2    = raw32 + (y-2)*W;
    const uint32_t* raw_y_p2    = raw32 + (y+2)*W;
    uint32_t* native = (bright_row ? bright : dark) + row_offset;
    uint32_t* interp = (bright_row ? dark : bright) + row_offset;
    int is_rg = (y % 2 == 0);
    int row_white_ev = raw2ev[bright_row ? d_white : d_white_darkened];
    int s = (d_is_bright[y&3] == d_is_bright[(y+1)&3]) ? -1 : 1;
    const uint32_t* raw_y_s   = raw32 + (y+s)*W;
    const uint32_t* raw_y_far = raw32 + (y-2*s)*W;

    if (is_rg) {
        int ri = mean2_noerr(raw2ev[raw_y_m2[x]], raw2ev[raw_y_p2[x]], row_white_ev);
        int gi = mean3_noerr(raw2ev[raw_y_s[x+2]], raw2ev[raw_y_s[x]], raw2ev[raw_y_far[x+1]], row_white_ev);
        interp[x]   = ev2raw[ri];
        interp[x+1] = ev2raw[gi];
        native[x]   = raw_row[x];
        native[x+1] = raw_row[x+1];
    } else {
        int bi = mean2_noerr(raw2ev[raw_y_m2[x+1]], raw2ev[raw_y_p2[x+1]], row_white_ev);
        int gi = mean3_noerr(raw2ev[raw_y_s[x+1]], raw2ev[raw_y_s[x-1]], raw2ev[raw_y_far[x]], row_white_ev);
        interp[x]   = ev2raw[gi];
        interp[x+1] = ev2raw[bi];
        native[x]   = raw_row[x];
        native[x+1] = raw_row[x+1];
    }
}

/* STAGE 4: border_interpolate (top rows, bottom rows, then cols - scalar order) */
__global__ void k_border_toprows(const uint32_t* __restrict raw32,
                                 uint32_t* __restrict dark, uint32_t* __restrict bright,
                                 int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;   /* 0..2 */
    if (x>=W || y>=3) return;
    int br = d_is_bright[y & 3];
    uint32_t* native = br ? bright : dark;
    uint32_t* interp = br ? dark : bright;
    interp[x + y*W] = raw32[x + (y+2)*W];
    native[x + y*W] = raw32[x + y*W];
}
__global__ void k_border_botrows(const uint32_t* __restrict raw32,
                                 uint32_t* __restrict dark, uint32_t* __restrict bright,
                                 int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int yy = blockIdx.y*blockDim.y + threadIdx.y;  /* 0..3 -> y = H-4+yy */
    if (x>=W || yy>=4) return;
    int y = H-4+yy;
    int br = d_is_bright[y & 3];
    uint32_t* native = br ? bright : dark;
    uint32_t* interp = br ? dark : bright;
    interp[x + y*W] = raw32[x + (y-2)*W];
    native[x + y*W] = raw32[x + y*W];
}
__global__ void k_border_cols(const uint32_t* __restrict raw32,
                              uint32_t* __restrict dark, uint32_t* __restrict bright,
                              int W, int H)
{
    int y = blockIdx.x*blockDim.x + threadIdx.x;   /* 2..H-1 */
    if (y<2 || y>=H) return;
    int br = d_is_bright[y & 3];
    uint32_t* native = br ? bright : dark;
    uint32_t* interp = br ? dark : bright;
    for (int x=0; x<2; x++) {
        interp[x + y*W] = raw32[x + (y-2)*W];
        native[x + y*W] = raw32[x + y*W];
    }
    for (int x=W-3; x<W; x++) {
        interp[x + y*W] = raw32[(x-2) + (y-2)*W];
        native[x + y*W] = raw32[(x-2) + y*W];
    }
}

/* STAGE 5: fullres_reconstruction */
__global__ void k_fullres(uint32_t* __restrict fullres,
                          const uint32_t* __restrict dark, const uint32_t* __restrict bright,
                          int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    if (d_is_bright[y & 3]) {
        uint32_t f = bright[i];
        fullres[i] = (f < (uint32_t)d_white_darkened) ? f : (uint32_t)imax((int)f, (int)dark[i]);
    } else {
        fullres[i] = dark[i];
    }
}

__device__ static inline int raw20_to_ev(const int* __restrict raw2ev, uint32_t raw)
{
    return raw2ev[raw > 0xFFFFFu ? 0xFFFFF : (int)raw];
}

__global__ void k_fullres_phase_balance(uint32_t* __restrict fullres,
                                         const uint32_t* __restrict dark,
                                         const uint32_t* __restrict bright,
                                         const int* __restrict raw2ev,
                                         const int* __restrict ev2raw,
                                         int W, int H)
{
    const int x = blockIdx.x*blockDim.x + threadIdx.x;
    const int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    const size_t i=(size_t)x+(size_t)y*W;
    const int bright_row = d_is_bright[y & 3];
    const uint32_t native = bright_row ? bright[i] : dark[i];
    const uint32_t opposite = bright_row ? dark[i] : bright[i];

    if (bright[i] >= (uint32_t)d_white_darkened) {
        fullres[i] = bright_row ? (uint32_t)imax((int)native, (int)opposite) : native;
        return;
    }

    const int native_weight = 1;
    const int opposite_weight = ((x ^ y) & 1) ? 3 : 1;
    const int denom = native_weight + opposite_weight;
    int ev = (raw20_to_ev(raw2ev, native) * native_weight +
              raw20_to_ev(raw2ev, opposite) * opposite_weight +
              denom / 2) / denom;
    ev = icoerce(ev, -10 * EV_RESOLUTION, 14 * EV_RESOLUTION - 1);
    fullres[i] = (uint32_t)ev2raw[ev];
}

/* STAGE 6: mix_images halfres blend */
__global__ void k_mix_halfres(uint32_t* __restrict halfres,
                              const uint32_t* __restrict bright, const uint32_t* __restrict dark,
                              const int* __restrict raw2ev, const int* __restrict ev2raw,
                              const double* __restrict mix_curve,
                              int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    int b = (int)bright[i];
    int d = (int)dark[i];
    int bev = raw2ev[b];
    int dev = raw2ev[d];
    double k = dcoerce(mix_curve[b & 0xFFFFF], 0.0, 1.0);
    int mixed = (int)((double)bev*(1.0-k) + (double)dev*k);
    halfres[i] = ev2raw[mixed];
}

__global__ void k_mix_halfres_avx2(uint32_t* __restrict halfres,
                                   const uint32_t* __restrict bright,
                                   const uint32_t* __restrict dark,
                                   const int* __restrict raw2ev,
                                   const int* __restrict ev2raw,
                                   const double* __restrict mix_curve,
                                   int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;

    int b = (int)bright[i];
    int d = (int)dark[i];
    float bev = (float)raw2ev[b];
    float dev = (float)raw2ev[d];
    float k = (float)mix_curve[b & 0xFFFFF];
    k = fminf(fmaxf(k, 0.0f), 1.0f);

    float mixed_f = fmaf(k, dev - bev, bev);
    int mixed = __float2int_rz(mixed_f);
    mixed = icoerce(mixed, -10*EV_RESOLUTION, 14*EV_RESOLUTION - 1);
    halfres[i] = ev2raw[mixed];
}

__device__ static inline int chroma_med5(int a0, int a1, int a2, int a3, int a4)
{
    #define CS2_SWAP(lo, hi) do { \
        const int _lo = imin((lo), (hi)); \
        const int _hi = imax((lo), (hi)); \
        (lo) = _lo; \
        (hi) = _hi; \
    } while (0)

    CS2_SWAP(a0, a1);
    CS2_SWAP(a3, a4);
    CS2_SWAP(a0, a3);
    CS2_SWAP(a1, a4);
    CS2_SWAP(a1, a2);
    CS2_SWAP(a2, a3);
    CS2_SWAP(a1, a2);

    #undef CS2_SWAP
    return a2;
}

__device__ static inline uint32_t chroma_ev2raw_lookup(const int* __restrict ev2raw, int ev)
{
    ev = icoerce(ev, -10 * EV_RESOLUTION, 14 * EV_RESOLUTION - 1);
    return (uint32_t)ev2raw[ev];
}

__device__ static inline void chroma_sample_h(const uint32_t* __restrict row0,
                                              const uint32_t* __restrict row1,
                                              int x0,
                                              const int* __restrict raw2ev,
                                              int* eh,
                                              int* med_r,
                                              int* med_b,
                                              int k)
{
    const int r = (int)row0[x0];
    const int b = (int)row1[x0 + 1];
    const int rr = raw2ev[r];
    const int bb = raw2ev[b];
    const int g1 = raw2ev[row0[x0 + 1]];
    const int g2 = raw2ev[row1[x0]];
    const int g3 = raw2ev[row0[x0 - 1]];
    const int g5 = raw2ev[row1[x0 + 2]];
    const int gr = (g1 + g3) / 2;
    const int gb = (g2 + g5) / 2;
    *eh += iabs_(g1 - g3) + iabs_(g2 - g5);
    med_r[k] = rr - gr;
    med_b[k] = bb - gb;
}

__device__ static inline void chroma_sample_v(const uint32_t* __restrict row0,
                                              const uint32_t* __restrict row1,
                                              const uint32_t* __restrict rowm1,
                                              const uint32_t* __restrict rowp2,
                                              int x0,
                                              const int* __restrict raw2ev,
                                              int* ev,
                                              int* med_r,
                                              int* med_b,
                                              int k)
{
    const int r = (int)row0[x0];
    const int b = (int)row1[x0 + 1];
    const int rr = raw2ev[r];
    const int bb = raw2ev[b];
    const int g1 = raw2ev[row0[x0 + 1]];
    const int g2 = raw2ev[row1[x0]];
    const int g4 = raw2ev[rowm1[x0]];
    const int g6 = raw2ev[rowp2[x0 + 1]];
    const int gr = (g2 + g4) / 2;
    const int gb = (g1 + g6) / 2;
    *ev += iabs_(g2 - g4) + iabs_(g1 - g6);
    med_r[k] = rr - gr;
    med_b[k] = bb - gb;
}

/* Optional 2x2 chroma smoothing, matching hdr_chroma_smooth(... method=1).
 * Input is copied to output before this kernel, so borders and greens remain
 * unchanged just like the CPU scratch flow. */
__global__ void k_chroma_smooth_2x2(const uint32_t* __restrict inp,
                                    uint32_t* __restrict out,
                                    const int* __restrict raw2ev,
                                    const int* __restrict ev2raw,
                                    int white_level,
                                    int W, int H)
{
    const int x = 4 + 2 * (blockIdx.x * blockDim.x + threadIdx.x);
    const int y = 4 + 2 * (blockIdx.y * blockDim.y + threadIdx.y);
    if (x >= W - 4 || y >= H - 5) return;

    const uint32_t* row_y_m3 = inp + (size_t)(y - 3) * (size_t)W;
    const uint32_t* row_y_m2 = inp + (size_t)(y - 2) * (size_t)W;
    const uint32_t* row_y_m1 = inp + (size_t)(y - 1) * (size_t)W;
    const uint32_t* row_y    = inp + (size_t)y * (size_t)W;
    const uint32_t* row_y_p1 = row_y + W;
    const uint32_t* row_y_p2 = inp + (size_t)(y + 2) * (size_t)W;
    const uint32_t* row_y_p3 = inp + (size_t)(y + 3) * (size_t)W;
    const uint32_t* row_y_p4 = inp + (size_t)(y + 4) * (size_t)W;
    uint32_t* out_y = out + (size_t)y * (size_t)W;
    uint32_t* out_y_p1 = out_y + W;

    const int r0 = (int)row_y[x];
    const int b0 = (int)row_y_p1[x + 1];
    const unsigned int white_u = (unsigned int)white_level;
    const int write_r = (unsigned int)r0 < white_u;
    const int write_b = (unsigned int)b0 < white_u;
    if (!write_r && !write_b) return;

    int med_r[5];
    int med_b[5];
    int eh = 0;
    int ev = 0;

    chroma_sample_h(row_y,    row_y_p1, x - 2, raw2ev, &eh, med_r, med_b, 0);
    chroma_sample_h(row_y_m2, row_y_m1, x,     raw2ev, &eh, med_r, med_b, 1);
    chroma_sample_h(row_y,    row_y_p1, x,     raw2ev, &eh, med_r, med_b, 2);
    chroma_sample_h(row_y_p2, row_y_p3, x,     raw2ev, &eh, med_r, med_b, 3);
    chroma_sample_h(row_y,    row_y_p1, x + 2, raw2ev, &eh, med_r, med_b, 4);
    const int drh = chroma_med5(med_r[0], med_r[1], med_r[2], med_r[3], med_r[4]);
    const int dbh = chroma_med5(med_b[0], med_b[1], med_b[2], med_b[3], med_b[4]);

    chroma_sample_v(row_y,    row_y_p1, row_y_m1, row_y_p2, x - 2, raw2ev, &ev, med_r, med_b, 0);
    chroma_sample_v(row_y_m2, row_y_m1, row_y_m3, row_y,    x,     raw2ev, &ev, med_r, med_b, 1);
    chroma_sample_v(row_y,    row_y_p1, row_y_m1, row_y_p2, x,     raw2ev, &ev, med_r, med_b, 2);
    chroma_sample_v(row_y_p2, row_y_p3, row_y_p1, row_y_p4, x,     raw2ev, &ev, med_r, med_b, 3);
    chroma_sample_v(row_y,    row_y_p1, row_y_m1, row_y_p2, x + 2, raw2ev, &ev, med_r, med_b, 4);
    const int drv = chroma_med5(med_r[0], med_r[1], med_r[2], med_r[3], med_r[4]);
    const int dbv = chroma_med5(med_b[0], med_b[1], med_b[2], med_b[3], med_b[4]);

    const int g1 = raw2ev[row_y[x + 1]];
    const int g2 = raw2ev[row_y_p1[x]];
    const int g3 = raw2ev[row_y[x - 1]];
    const int g4 = raw2ev[row_y_m1[x]];
    const int g5 = raw2ev[row_y_p1[x + 2]];
    const int g6 = raw2ev[row_y_p2[x + 1]];

    const int grv = (g2 + g4) / 2;
    const int grh = (g1 + g3) / 2;
    const int gbv = (g1 + g6) / 2;
    const int gbh = (g2 + g5) / 2;
    const int choose_ev_lt_eh = ev < eh;
    int dr = choose_ev_lt_eh ? drv : drh;
    int db = choose_ev_lt_eh ? dbv : dbh;

    const int direction_ev_thr = 64;
    const int black_thr = d_black + 64 * 64;
    const int use_average =
        r0 < black_thr ||
        b0 < black_thr ||
        iabs_(drv - drh) < direction_ev_thr ||
        iabs_(grv - grh) < direction_ev_thr ||
        iabs_(gbv - gbh) < direction_ev_thr;

    int gr = 0;
    int gb = 0;
    if (use_average) {
        if (write_r) {
            dr = (drv + drh) / 2;
            gr = (g1 + g2 + g3 + g4) / 4;
        }
        if (write_b) {
            db = (dbv + dbh) / 2;
            gb = (g1 + g2 + g5 + g6) / 4;
        }
    } else if (choose_ev_lt_eh) {
        if (write_r) gr = grv;
        if (write_b) gb = gbv;
    } else {
        if (write_r) gr = grh;
        if (write_b) gb = gbh;
    }

    if (write_r) out_y[x] = chroma_ev2raw_lookup(ev2raw, gr + dr);
    if (write_b) out_y_p1[x + 1] = chroma_ev2raw_lookup(ev2raw, gb + db);
}

/* STAGE 7: build_alias_map */
__global__ void k_alias_init(uint16_t* __restrict alias_map,
                             const uint32_t* __restrict fullres_smooth,
                             const uint32_t* __restrict halfres_smooth,
                             const uint32_t* __restrict bright,
                             const int* __restrict raw2ev,
                             const double* __restrict fullres_curve_d,
                             int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    if (fullres_curve_d[bright[i]] > HOST_FULLRES_THR) return;
    int f = (int)fullres_smooth[i];
    int hh = (int)halfres_smooth[i];
    int fe = raw2ev[f];
    int he = raw2ev[hh];
    int e_lin = iabs_(f - hh);
    e_lin = imax(e_lin - d_dark_noise*3/2, 0);
    int e_log = iabs_(fe - he);
    alias_map[i] = (uint16_t)imin(imin(e_lin/2, e_log/16), 65530);
}

#define TOP5(value) do { \
    uint16_t av=(uint16_t)(value); \
    if (av>best4){ if (av>best3){ best4=best3; \
        if (av>best2){ best3=best2; \
            if (av>best1){ best2=best1; \
                if (av>best0){ best1=best0; best0=av; } else { best1=av; } \
            } else { best2=av; } \
        } else { best3=av; } \
    } else { best4=av; } } \
} while(0)
#define TOP5R(row,off) TOP5((row)[x + (off)])
__global__ void k_alias_rank(const uint16_t* __restrict alias_map,
                             uint16_t* __restrict alias_aux,
                             const uint32_t* __restrict bright,
                             const double* __restrict fullres_curve_d,
                             int W, int H)
{
    int x = 6 + blockIdx.x*blockDim.x + threadIdx.x;
    int y = 6 + blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W-6 || y>=H-6) return;
    if (fullres_curve_d[bright[x + (size_t)y*W]] > HOST_FULLRES_THR) return;

    uint16_t best0=0,best1=0,best2=0,best3=0,best4=0;
    const uint16_t* ym6 = alias_map + (size_t)(y-6)*W;
    const uint16_t* ym4 = alias_map + (size_t)(y-4)*W;
    const uint16_t* ym2 = alias_map + (size_t)(y-2)*W;
    const uint16_t* y0  = alias_map + (size_t)(y  )*W;
    const uint16_t* yp2 = alias_map + (size_t)(y+2)*W;
    const uint16_t* yp4 = alias_map + (size_t)(y+4)*W;
    const uint16_t* yp6 = alias_map + (size_t)(y+6)*W;
                       TOP5R(ym6,-2); TOP5R(ym6,0); TOP5R(ym6,2);
        TOP5R(ym4,-4); TOP5R(ym4,-2); TOP5R(ym4,0); TOP5R(ym4,2); TOP5R(ym4,4);
    TOP5R(ym2,-6);TOP5R(ym2,-4);TOP5R(ym2,-2);TOP5R(ym2,0);TOP5R(ym2,2);TOP5R(ym2,4);TOP5R(ym2,6);
    TOP5R(y0, -6);TOP5R(y0, -4);TOP5R(y0, -2);TOP5R(y0, 0);TOP5R(y0, 2);TOP5R(y0, 4);TOP5R(y0, 6);
    TOP5R(yp2,-6);TOP5R(yp2,-4);TOP5R(yp2,-2);TOP5R(yp2,0);TOP5R(yp2,2);TOP5R(yp2,4);TOP5R(yp2,6);
        TOP5R(yp4,-4); TOP5R(yp4,-2); TOP5R(yp4,0); TOP5R(yp4,2); TOP5R(yp4,4);
                       TOP5R(yp6,-2); TOP5R(yp6,0); TOP5R(yp6,2);
    alias_aux[x + (size_t)y*W] = best4;
}

__global__ void k_alias_gauss(uint16_t* __restrict alias_map,
                              const uint16_t* __restrict alias_aux,
                              const uint32_t* __restrict bright,
                              const double* __restrict fullres_curve_d,
                              int W, int H)
{
    int x = 6 + blockIdx.x*blockDim.x + threadIdx.x;
    int y = 6 + blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W-6 || y>=H-6) return;
    if (fullres_curve_d[bright[x + (size_t)y*W]] > HOST_FULLRES_THR) return;
    const uint16_t* A = alias_aux;
    #define AX(dx,dy) ((int)A[(x+(dx)) + (size_t)(y+(dy))*W])
    int c =
    (AX(0,0))
    + (AX(0,-2)+AX(-2,0)+AX(2,0)+AX(0,2)) * 820 / 1024
    + (AX(-2,-2)+AX(2,-2)+AX(-2,2)+AX(2,2)) * 657 / 1024
    + (AX(0,-2)+AX(-2,0)+AX(2,0)+AX(0,2)) * 421 / 1024
    + (AX(-2,-2)+AX(2,-2)+AX(-2,-2)+AX(2,-2)+AX(-2,2)+AX(2,2)+AX(-2,2)+AX(2,2)) * 337 / 1024
    + (AX(-2,-2)+AX(2,-2)+AX(-2,2)+AX(2,2)) * 173 / 1024
    + (AX(0,-6)+AX(-6,0)+AX(6,0)+AX(0,6)) * 139 / 1024
    + (AX(-2,-6)+AX(2,-6)+AX(-6,-2)+AX(6,-2)+AX(-6,2)+AX(6,2)+AX(-2,6)+AX(2,6)) * 111 / 1024
    + (AX(-2,-6)+AX(2,-6)+AX(-6,-2)+AX(6,-2)+AX(-6,2)+AX(6,2)+AX(-2,6)+AX(2,6)) * 57 / 1024;
    #undef AX
    alias_map[x + (size_t)y*W] = (uint16_t)c;
}

__global__ void k_alias_gray(uint16_t* __restrict alias_map, int W, int H)
{
    int x = 2 + 2*(blockIdx.x*blockDim.x + threadIdx.x);
    int y = 2 + 2*(blockIdx.y*blockDim.y + threadIdx.y);
    if (x>=W-2 || y>=H-2) return;
    int a = alias_map[x   +     y*W];
    int b = alias_map[x+1 +     y*W];
    int c = alias_map[x   + (y+1)*W];
    int d = alias_map[x+1 + (y+1)*W];
    int C = imax(imax(a,b), imax(c,d));
    C = imin(C, ALIAS_MAP_MAX);
    alias_map[x   +     y*W] = (uint16_t)C;
    alias_map[x+1 +     y*W] = (uint16_t)C;
    alias_map[x   + (y+1)*W] = (uint16_t)C;
    alias_map[x+1 + (y+1)*W] = (uint16_t)C;
}

/* STAGE 8: overexposed map */
__global__ void k_over_mark(uint16_t* __restrict over_aux,
                            const uint32_t* __restrict bright, const uint32_t* __restrict dark,
                            int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    over_aux[i] = (bright[i] >= (uint32_t)d_white_darkened || dark[i] >= (uint32_t)d_white) ? 100 : 0;
}
__global__ void k_over_bordercopy(uint16_t* __restrict overexposed,
                                  const uint16_t* __restrict over_aux,
                                  int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    if (y<3 || y>=H-3) { overexposed[i] = over_aux[i]; return; }
    if (x<3 || x>=W-3) { overexposed[i] = over_aux[i]; return; }
    /* interior handled by blur kernel */
}
__global__ void k_over_blur(uint16_t* __restrict overexposed,
                            const uint16_t* __restrict over_aux,
                            int W, int H)
{
    int x = 3 + blockIdx.x*blockDim.x + threadIdx.x;
    int y = 3 + blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W-3 || y>=H-3) return;
    const uint16_t* O = over_aux;
    #define OX(dx,dy) ((int)O[(x+(dx)) + (size_t)(y+(dy))*W])
    int v =
    (OX(0,0))
    + (OX(0,-1)+OX(-1,0)+OX(1,0)+OX(0,1)) * 820 / 1024
    + (OX(-1,-1)+OX(1,-1)+OX(-1,1)+OX(1,1)) * 657 / 1024
    + 0;
    #undef OX
    overexposed[x + (size_t)y*W] = (uint16_t)v;
}

/* STAGE 9: final_blend -> raw_buffer_32 (20-bit) */
__global__ void k_final_blend(uint32_t* __restrict raw32,
                              const uint32_t* __restrict bright,
                              const uint32_t* __restrict halfres_smooth,
                              const uint32_t* __restrict fullres,
                              const uint32_t* __restrict fullres_smooth,
                              const uint32_t* __restrict dark,
                              const uint16_t* __restrict overexposed,
                              const uint16_t* __restrict alias_map,
                              const int* __restrict raw2ev, const int* __restrict ev2raw,
                              const double* __restrict fullres_curve_f,
                              int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;

    int b   = (int)bright[i];
    int hr  = (int)halfres_smooth[i];
    int fr  = (int)fullres[i];
    int frs = (int)fullres_smooth[i];
    int hrev  = raw2ev[hr];
    int frev  = raw2ev[fr];
    int frsev = raw2ev[frs];

    double f = (double)(float)fullres_curve_f[b & 0xFFFFF];

    double c = 0.0;
    {
        int co = (int)alias_map[i];
        c = dcoerce((double)co / (double)ALIAS_MAP_MAX, 0.0, 1.0);
    }
    double ovf = dcoerce((double)overexposed[i] / 200.0, 0.0, 1.0);
    c = dmax(c, ovf);

    double noisy_or_overexposed = dmax(ovf, 1.0 - f);
    f = dmax(f, c);
    double fev = noisy_or_overexposed * (double)frsev + (1.0 - noisy_or_overexposed) * (double)frev;

    int sig = ((int)dark[i] + (int)bright[i]) / 2;
    f = dmax(0.0, dmin(f, (double)(sig - d_black) / (double)(4*d_dark_noise)));

    int output = (int)((double)hrev * (1.0 - f) + fev * f);
    output = icoerce(output, -10*EV_RESOLUTION, 14*EV_RESOLUTION - 1);
    raw32[i] = (uint32_t)ev2raw[output];
}

/* STAGE 10: convert_20_to_16bit (dither off) */
__global__ void k_convert16(uint16_t* __restrict out, const uint32_t* __restrict raw32,
                            int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    double v = (double)raw32[i];
    int o = (int)(v/16.0 + 0.5);
    out[i] = (uint16_t)icoerce(o, 0, 0xFFFF);
}

__device__ static inline uint32_t ev_to_raw20(const int* __restrict ev2raw, int ev)
{
    ev = icoerce(ev, -10 * EV_RESOLUTION, 14 * EV_RESOLUTION - 1);
    return (uint32_t)ev2raw[ev];
}

/* CPU x1 playback mesh guard path. Mirrors final_blend(... fullres_mesh_guard=1)
 * and writes raw32 so the post-blend stabilizers can run before 16-bit convert. */
__global__ void k_final_blend_mesh_guard(uint32_t* __restrict raw32,
                                         const uint32_t* __restrict bright,
                                         const uint32_t* __restrict halfres_smooth,
                                         const uint32_t* __restrict fullres,
                                         const uint32_t* __restrict fullres_smooth,
                                         const uint32_t* __restrict dark,
                                         const uint16_t* __restrict overexposed,
                                         const uint16_t* __restrict alias_map,
                                         const int* __restrict raw2ev,
                                         const int* __restrict ev2raw,
                                         const double* __restrict fullres_curve_f,
                                         int W, int H)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;
    const size_t i = (size_t)x + (size_t)y * (size_t)W;

    const int b = (int)bright[i];
    const int hr = (int)halfres_smooth[i];
    const int fr = (int)fullres[i];
    const int frs = (int)fullres_smooth[i];
    const int hrev = raw2ev[hr];
    const int frev = raw2ev[fr];
    const int frsev = raw2ev[frs];

    double f = (double)(float)fullres_curve_f[b & 0xFFFFF];
    double c = 0.0;
    if (alias_map) {
        const int co = (int)alias_map[i];
        c = dcoerce((double)co / (double)ALIAS_MAP_MAX, 0.0, 1.0);
    }

    const double ovf = dcoerce((double)overexposed[i] / 200.0, 0.0, 1.0);
    c = dmax(c, ovf);
    f = dmax(f, c);

    if (x > 1 && x + 2 < W && y > 1 && y + 2 < H) {
        const int full_left_ev = raw2ev[fullres[i - 2]];
        const int full_right_ev = raw2ev[fullres[i + 2]];
        const int full_up_ev = raw2ev[fullres[i - (size_t)2 * (size_t)W]];
        const int full_down_ev = raw2ev[fullres[i + (size_t)2 * (size_t)W]];
        const int half_left2_ev = raw2ev[halfres_smooth[i - 2]];
        const int half_right2_ev = raw2ev[halfres_smooth[i + 2]];
        const int half_up2_ev = raw2ev[halfres_smooth[i - (size_t)2 * (size_t)W]];
        const int half_down2_ev = raw2ev[halfres_smooth[i + (size_t)2 * (size_t)W]];
        const int full_axis_ev = imax(iabs_(full_left_ev - full_right_ev),
                                      iabs_(full_up_ev - full_down_ev));
        const int half_axis2_ev = imax(iabs_(half_left2_ev - half_right2_ev),
                                       iabs_(half_up2_ev - half_down2_ev));
        const int full_outlier_ev = imax(
            iabs_(frev - ((full_left_ev + full_right_ev + 1) / 2)),
            iabs_(frev - ((full_up_ev + full_down_ev + 1) / 2)));
        const int half_outlier_ev = imax(
            iabs_(hrev - ((half_left2_ev + half_right2_ev + 1) / 2)),
            iabs_(hrev - ((half_up2_ev + half_down2_ev + 1) / 2)));
        if (full_outlier_ev > half_outlier_ev + EV_RESOLUTION / 2 &&
            full_axis_ev > half_axis2_ev + EV_RESOLUTION / 2) {
            f = dmin(f, dmax(ovf, 0.005));
        } else if (full_outlier_ev > half_outlier_ev + EV_RESOLUTION / 4 &&
                   full_axis_ev > half_axis2_ev + EV_RESOLUTION / 4) {
            f = dmin(f, dmax(ovf, 0.015));
        }
    }

    if (x > 0 && x + 1 < W && y > 0 && y + 1 < H) {
        const int half_left_ev = raw2ev[halfres_smooth[i - 1]];
        const int half_right_ev = raw2ev[halfres_smooth[i + 1]];
        const int half_up_ev = raw2ev[halfres_smooth[i - (size_t)W]];
        const int half_down_ev = raw2ev[halfres_smooth[i + (size_t)W]];
        const int half_grad_ev = imax(iabs_(half_left_ev - half_right_ev),
                                      iabs_(half_up_ev - half_down_ev));
        double local_cap = 1.0;
        if (half_grad_ev < EV_RESOLUTION / 4) {
            local_cap = 0.005;
        } else if (half_grad_ev < EV_RESOLUTION) {
            local_cap = 0.015;
        } else if (half_grad_ev < EV_RESOLUTION * 2) {
            local_cap = 0.04;
        } else if (half_grad_ev < EV_RESOLUTION * 4) {
            local_cap = 0.10;
        }
        f = dmin(f, dmax(ovf, local_cap));
    }

    const double noisy_or_overexposed = dmax(ovf, 1.0 - f);
    const int anchor_ev = hrev;
    double fev = noisy_or_overexposed * (double)frsev +
                 (1.0 - noisy_or_overexposed) * (double)frev;

    if (x > 1 && x + 2 < W && y > 1 && y + 2 < H) {
        const int full_left_ev = raw2ev[fullres[i - 2]];
        const int full_right_ev = raw2ev[fullres[i + 2]];
        const int full_up_ev = raw2ev[fullres[i - (size_t)2 * (size_t)W]];
        const int full_down_ev = raw2ev[fullres[i + (size_t)2 * (size_t)W]];
        const int full_base_ev = (full_left_ev + full_right_ev +
                                  full_up_ev + full_down_ev + 2) / 4;
        const int half_left_ev = raw2ev[halfres_smooth[i - 2]];
        const int half_right_ev = raw2ev[halfres_smooth[i + 2]];
        const int half_up_ev = raw2ev[halfres_smooth[i - (size_t)2 * (size_t)W]];
        const int half_down_ev = raw2ev[halfres_smooth[i + (size_t)2 * (size_t)W]];
        const int half_base_ev = (half_left_ev + half_right_ev +
                                  half_up_ev + half_down_ev + 2) / 4;
        const int half_detail_ev = imax(
            imax(iabs_(hrev - half_base_ev), iabs_(half_left_ev - half_right_ev)),
            iabs_(half_up_ev - half_down_ev));
        const int detail_limit_ev = icoerce(half_detail_ev / 8 + EV_RESOLUTION / 32,
                                            EV_RESOLUTION / 64,
                                            EV_RESOLUTION / 4);
        const int green_site = ((x ^ y) & 1);
        const int full_detail_ev = green_site
            ? icoerce(frev - full_base_ev, -detail_limit_ev, detail_limit_ev)
            : 0;
        const int full_smooth_detail_ev = green_site
            ? icoerce(frsev - full_base_ev, -detail_limit_ev, detail_limit_ev)
            : 0;
        const int anchored_full_ev = anchor_ev + full_detail_ev;
        const int anchored_smooth_ev = anchor_ev + full_smooth_detail_ev;
        fev = noisy_or_overexposed * (double)anchored_smooth_ev +
              (1.0 - noisy_or_overexposed) * (double)anchored_full_ev;
    }

    const int sig = ((int)dark[i] + b) / 2;
    const double cap = (double)(sig - d_black) / (double)(4 * d_dark_noise);
    f = dmax(0.0, dmin(f, cap));

    int output = (int)((double)anchor_ev * (1.0 - f) + fev * f);
    raw32[i] = ev_to_raw20(ev2raw, output);
}

__global__ void k_output_vertical_mesh_stabilize(uint32_t* __restrict image,
                                                 const uint32_t* __restrict source,
                                                 const int* __restrict raw2ev,
                                                 const int* __restrict ev2raw,
                                                 int W, int H)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = 2 + blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H - 2) return;
    const size_t idx = (size_t)x + (size_t)y * (size_t)W;
    const int up_ev = raw20_to_ev(raw2ev, source[idx - (size_t)2 * (size_t)W]);
    const int down_ev = raw20_to_ev(raw2ev, source[idx + (size_t)2 * (size_t)W]);
    const int same_color_edge_limit = (EV_RESOLUTION * 3) / 4;
    if (iabs_(up_ev - down_ev) > same_color_edge_limit) return;

    const int center_ev = raw20_to_ev(raw2ev, source[idx]);
    const int neighbor_ev = (up_ev + down_ev + 1) / 2;
    const int correction_floor = EV_RESOLUTION / 32;
    if (iabs_(center_ev - neighbor_ev) < correction_floor) return;

    const int corrected_ev = (center_ev + 2 * neighbor_ev + 1) / 3;
    image[idx] = ev_to_raw20(ev2raw, corrected_ev);
}

__global__ void k_source_mesh_stabilize(uint32_t* __restrict plane,
                                        const uint32_t* __restrict source,
                                        const int* __restrict raw2ev,
                                        const int* __restrict ev2raw,
                                        int W, int H)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;
    const size_t idx = (size_t)x + (size_t)y * (size_t)W;
    const int center_ev = raw20_to_ev(raw2ev, source[idx]);
    int weighted_ev = center_ev * 2;
    int weight = 2;
    const int same_color_edge_limit = (EV_RESOLUTION * 3) / 4;

    if (y >= 2 && y + 2 < H) {
        const int up_ev = raw20_to_ev(raw2ev, source[idx - (size_t)2 * (size_t)W]);
        const int down_ev = raw20_to_ev(raw2ev, source[idx + (size_t)2 * (size_t)W]);
        if (iabs_(up_ev - down_ev) <= same_color_edge_limit) {
            weighted_ev += ((up_ev + down_ev + 1) / 2) * 2;
            weight += 2;
        }
    }
    if (x >= 2 && x + 2 < W) {
        const int left_ev = raw20_to_ev(raw2ev, source[idx - 2]);
        const int right_ev = raw20_to_ev(raw2ev, source[idx + 2]);
        if (iabs_(left_ev - right_ev) <= same_color_edge_limit) {
            weighted_ev += ((left_ev + right_ev + 1) / 2) * 2;
            weight += 2;
        }
    }
    if (weight == 2) return;

    const int corrected_ev = (weighted_ev + weight / 2) / weight;
    const int correction_floor = EV_RESOLUTION / 32;
    if (iabs_(center_ev - corrected_ev) < correction_floor) return;
    plane[idx] = ev_to_raw20(ev2raw, corrected_ev);
}

__global__ void k_flat_green_cell_balance(uint32_t* __restrict image,
                                          const int* __restrict raw2ev,
                                          const int* __restrict ev2raw,
                                          int W, int H)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x < 2 || x >= W - 3 || y < 2 || y >= H - 3 || (x & 1) || (y & 1)) return;

    const size_t g_red_idx = (size_t)(x + 1) + (size_t)y * (size_t)W;
    const size_t g_blue_idx = (size_t)x + (size_t)(y + 1) * (size_t)W;
    const int g_red_left_ev = raw20_to_ev(raw2ev, image[g_red_idx - 2]);
    const int g_red_right_ev = raw20_to_ev(raw2ev, image[g_red_idx + 2]);
    const int g_red_up_ev = raw20_to_ev(raw2ev, image[g_red_idx - (size_t)2 * (size_t)W]);
    const int g_red_down_ev = raw20_to_ev(raw2ev, image[g_red_idx + (size_t)2 * (size_t)W]);
    const int g_blue_left_ev = raw20_to_ev(raw2ev, image[g_blue_idx - 2]);
    const int g_blue_right_ev = raw20_to_ev(raw2ev, image[g_blue_idx + 2]);
    const int g_blue_up_ev = raw20_to_ev(raw2ev, image[g_blue_idx - (size_t)2 * (size_t)W]);
    const int g_blue_down_ev = raw20_to_ev(raw2ev, image[g_blue_idx + (size_t)2 * (size_t)W]);
    const int red_green_axis_ev = imax(iabs_(g_red_left_ev - g_red_right_ev),
                                       iabs_(g_red_up_ev - g_red_down_ev));
    const int blue_green_axis_ev = imax(iabs_(g_blue_left_ev - g_blue_right_ev),
                                        iabs_(g_blue_up_ev - g_blue_down_ev));
    if (imax(red_green_axis_ev, blue_green_axis_ev) > EV_RESOLUTION * 3 / 2) return;

    const int g_red_ev = raw20_to_ev(raw2ev, image[g_red_idx]);
    const int g_blue_ev = raw20_to_ev(raw2ev, image[g_blue_idx]);
    const int delta = g_red_ev - g_blue_ev;
    if (iabs_(delta) <= EV_RESOLUTION / 64) return;

    const int max_green_correction = EV_RESOLUTION / 2;
    const int correction = icoerce(delta / 2,
                                   -max_green_correction,
                                   max_green_correction);
    image[g_red_idx] = ev_to_raw20(ev2raw, g_red_ev - correction);
    image[g_blue_idx] = ev_to_raw20(ev2raw, g_blue_ev + correction);
}

__global__ void k_flat_chroma_guard(uint32_t* __restrict image,
                                    const int* __restrict raw2ev,
                                    const int* __restrict ev2raw,
                                    int W, int H)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x < 1 || x >= W - 1 || y < 1 || y >= H - 1) return;
    const int red_site = ((x & 1) == 0) && ((y & 1) == 0);
    const int blue_site = ((x & 1) == 1) && ((y & 1) == 1);
    if (!red_site && !blue_site) return;

    const size_t idx = (size_t)x + (size_t)y * (size_t)W;
    const int left_green_ev = raw20_to_ev(raw2ev, image[idx - 1]);
    const int right_green_ev = raw20_to_ev(raw2ev, image[idx + 1]);
    const int up_green_ev = raw20_to_ev(raw2ev, image[idx - (size_t)W]);
    const int down_green_ev = raw20_to_ev(raw2ev, image[idx + (size_t)W]);
    const int green_min = imin(imin(left_green_ev, right_green_ev),
                               imin(up_green_ev, down_green_ev));
    const int green_max = imax(imax(left_green_ev, right_green_ev),
                               imax(up_green_ev, down_green_ev));
    if (green_max - green_min > EV_RESOLUTION * 3 / 2) return;

    const int green_avg = (left_green_ev + right_green_ev +
                           up_green_ev + down_green_ev + 2) / 4;
    const int site_ev = raw20_to_ev(raw2ev, image[idx]);
    const int target_ev = green_avg + (red_site ? 0 : -EV_RESOLUTION / 4);
    const int excess = site_ev - target_ev;
    if (excess <= EV_RESOLUTION / 32) return;

    const int correction = imin(excess, EV_RESOLUTION * 2);
    image[idx] = ev_to_raw20(ev2raw, site_ev - correction);
}

/* STAGE 9+10: AVX2-compatible final_blend fused directly to 16-bit.
 * Mirrors final_blend_row_avx2(... fuse_to_16bit=1): float raw2ev values,
 * explicit fused multiply-adds, and per-row dither cursor `(y*7 + x) & 1023`.
 */
__global__ void k_final_blend16_avx2(uint16_t* __restrict out,
                                     const uint32_t* __restrict bright,
                                     const uint32_t* __restrict halfres_smooth,
                                     const uint32_t* __restrict fullres,
                                     const uint32_t* __restrict fullres_smooth,
                                     const uint32_t* __restrict dark,
                                     const uint16_t* __restrict overexposed,
                                     const uint16_t* __restrict alias_map,
                                     const int* __restrict raw2ev,
                                     const int* __restrict ev2raw,
                                     const double* __restrict fullres_curve_f,
                                     const float* __restrict randn05,
                                     int W, int H)
{
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;

    int b   = (int)bright[i];
    int hr  = (int)halfres_smooth[i];
    int fr  = (int)fullres[i];
    int frs = (int)fullres_smooth[i];
    int d   = (int)dark[i];

    float hrev  = (float)raw2ev[hr];
    float frev  = (float)raw2ev[fr];
    float frsev = (float)raw2ev[frs];
    float f = (float)fullres_curve_f[b & 0xFFFFF];

    int co = (int)alias_map[i];
    float c_amap = (float)co * (1.0f / (float)ALIAS_MAP_MAX);
    c_amap = fminf(fmaxf(c_amap, 0.0f), 1.0f);

    float ovf = (float)overexposed[i] * (1.0f / 200.0f);
    ovf = fminf(fmaxf(ovf, 0.0f), 1.0f);

    float c = fmaxf(c_amap, ovf);
    float noisy_or_over = fmaxf(ovf, 1.0f - f);
    f = fmaxf(f, c);

    float fev = fmaf(noisy_or_over, frsev - frev, frev);

    int sig = (d + b) >> 1;
    float cap = (float)(sig - d_black) * (1.0f / (4.0f * (float)d_dark_noise));
    f = fmaxf(0.0f, fminf(f, cap));

    float outf = fmaf(f, fev - hrev, hrev);
    int output = __float2int_rz(outf);
    output = icoerce(output, -10*EV_RESOLUTION, 14*EV_RESOLUTION - 1);

    int raw20 = ev2raw[output];
    float dither = randn05[(y * 7 + x) & 1023];
    float rounded = fmaf((float)raw20, 1.0f / 16.0f, dither);
    rounded += 0.5f;
    int v = __float2int_rz(rounded);
    out[i] = (uint16_t)icoerce(v, 0, 0xFFFF);
}

/* ===================================================================== *
 *  BACKEND HANDLE + ABI                                                  *
 * ===================================================================== */

struct CachedGlImageResource {
    cudaGraphicsResource* resource;
    unsigned int texture;
    int width;
    int height;
    unsigned int flags;
};

struct PreuploadSlot {
    uint16_t* host_input;
    uint16_t* device_input;
    size_t bytes;
    uint64_t frame_id;
    uint64_t sequence;
    int state; /* 0 free, 1 queued, 2 consumed by a run, 3 staging */
    int submitted_while_prior_run_active;
    double host_staging_ms;
    cudaEvent_t upload_start;
    cudaEvent_t upload_ready;
};

struct igpu_recon_backend {
    int   device;
    char  describe[256];

    /* clip geometry / static levels */
    int   width, height;
    int   black, white;
    int   is_bright[4];
    int   have_clip;
    int   have_luts;

    /* device buffers (W*H sized; allocated on set_clip) */
    uint16_t *d_in, *d_out, *d_alias, *d_aliasaux, *d_over, *d_overaux;
    uint32_t *d_raw32, *d_dark, *d_bright, *d_fullres, *d_halfres;
    uint32_t *d_fullres_smooth, *d_halfres_smooth;
    uint32_t *d_chroma_tmp;
    uint64_t clip_allocated_bytes;
    uint64_t chroma_tmp_allocated_bytes;

    /* device LUTs (uploaded once on set_luts) */
    int      *dd_raw2ev, *dd_ev2raw;     /* ev2raw is BASE; origin = +EV2RAW_ORIGIN */
    double   *dd_mix, *dd_frc_f, *dd_frc_d;
    float    *df_randn05;
    uint64_t lut_allocated_bytes;

    /* timing */
    igpu_recon_timing_t last_timing;

    /* cached CUDA-GL interop resource for the live no-readback R16 output */
    CachedGlImageResource gl_r16_resource;

    /* Optional retained device outputs for cross-thread live playback handoff.
     * Each slot is backend-owned CUDA memory holding a copy of d_out. */
    uint16_t* retained_device_output[RETAINED_DEVICE_OUTPUT_SLOTS];
    uint64_t retained_device_output_token[RETAINED_DEVICE_OUTPUT_SLOTS];
    int retained_device_output_in_use[RETAINED_DEVICE_OUTPUT_SLOTS];
    int retained_device_output_width[RETAINED_DEVICE_OUTPUT_SLOTS];
    int retained_device_output_height[RETAINED_DEVICE_OUTPUT_SLOTS];
    uint64_t retained_device_output_allocated_bytes;
    uint64_t next_retained_device_output_token;

    /* Playback-only N+1 H2D lookahead.  The upload stream is non-blocking so
     * legacy-default-stream kernels for frame N do not serialize frame N+1. */
    PreuploadSlot preupload[PREUPLOAD_SLOTS];
    cudaStream_t preupload_stream;
    CRITICAL_SECTION preupload_lock;
    int preupload_lock_initialized;
    uint64_t next_preupload_sequence;
    uint64_t preupload_device_allocated_bytes;
    igpu_recon_preupload_status_t last_preupload_status;

    cudaEvent_t ev_start, ev_after_up, ev_after_kernel, ev_after_dl;
};

static double qpc_now_ms()
{
    LARGE_INTEGER counter;
    LARGE_INTEGER frequency;
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    return frequency.QuadPart > 0
        ? (1000.0 * (double)counter.QuadPart / (double)frequency.QuadPart)
        : 0.0;
}

static int debug_dump_blob(const char* dir,
                           const char* name,
                           const void* data,
                           size_t bytes)
{
    if (!dir || !*dir || !name || !*name || !data) return 0;
#ifdef _WIN32
    CreateDirectoryA(dir, NULL);
#endif
    char path[MAX_PATH * 4];
    snprintf(path, sizeof(path), "%s\\%s", dir, name);
    FILE* f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "[igpu_recon_cuda] debug dump cannot open %s\n", path);
        return -1;
    }
    const size_t wrote = fwrite(data, 1, bytes, f);
    fclose(f);
    if (wrote != bytes) {
        fprintf(stderr, "[igpu_recon_cuda] debug dump short write %s\n", path);
        return -1;
    }
    return 0;
}

static int debug_dump_device_blob(const char* dir,
                                  const char* name,
                                  const void* device,
                                  size_t bytes)
{
    if (!dir || !*dir || !name || !*name || !device || bytes == 0) return 0;
    void* host = malloc(bytes);
    if (!host) {
        fprintf(stderr, "[igpu_recon_cuda] debug dump malloc failed for %s (%zu bytes)\n",
                name, bytes);
        return -1;
    }
    const cudaError_t e = cudaMemcpy(host, device, bytes, cudaMemcpyDeviceToHost);
    if (e != cudaSuccess) {
        fprintf(stderr, "[igpu_recon_cuda] debug dump cudaMemcpy(%s) failed: %s\n",
                name, cudaGetErrorString(e));
        free(host);
        return -1;
    }
    const int rc = debug_dump_blob(dir, name, host, bytes);
    free(host);
    return rc;
}

static void debug_dump_recon_stages(igpu_recon_backend* b,
                                    const igpu_recon_frame_t* frame,
                                    uint32_t* fullres_smooth,
                                    uint32_t* halfres_smooth)
{
    const char* dir = getenv("IGPU_RECON_DEBUG_DUMP_DIR");
    if (!dir || !*dir || !b || !frame) return;
    const size_t n = (size_t)b->width * (size_t)b->height;
    debug_dump_device_blob(dir, "cuda_stage_dark.u32", b->d_dark, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_bright.u32", b->d_bright, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_dark_source_smooth.u32",
                           b->d_halfres_smooth, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_bright_source_smooth.u32",
                           b->d_fullres_smooth, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_fullres.u32", b->d_fullres, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_halfres.u32", b->d_halfres, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_fullres_smooth.u32",
                           fullres_smooth, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_halfres_smooth.u32",
                           halfres_smooth, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_stage_alias_map.u16", b->d_alias, n * sizeof(uint16_t));
    debug_dump_device_blob(dir, "cuda_stage_overexposed.u16", b->d_over, n * sizeof(uint16_t));
    debug_dump_device_blob(dir, "cuda_stage_final20.u32", b->d_raw32, n * sizeof(uint32_t));
    debug_dump_device_blob(dir, "cuda_out.u16", b->d_out, n * sizeof(uint16_t));
    debug_dump_blob(dir, "cuda_debug_frame.bin", frame, sizeof(*frame));
}

static int reset_cached_gl_resource(CachedGlImageResource* cache,
                                    int tolerate_invalid_graphics_context)
{
    int status = 0;
    if (!cache) return status;
    if (cache->resource) {
        const cudaError_t rc = cudaGraphicsUnregisterResource(cache->resource);
        if (rc != cudaSuccess) {
            if (!(tolerate_invalid_graphics_context
                  && rc == cudaErrorInvalidGraphicsContext)) {
                fprintf(stderr,
                        "[igpu_recon_cuda] cudaGraphicsUnregisterResource(cached GL_R16 texture) failed: %s\n",
                        cudaGetErrorString(rc));
            }
            status = 2;
        }
    }
    cache->resource = NULL;
    cache->texture = 0;
    cache->width = 0;
    cache->height = 0;
    cache->flags = 0;
    return status;
}

static int ensure_cached_gl_resource(CachedGlImageResource* cache,
                                     unsigned int gl_texture,
                                     int width,
                                     int height,
                                     unsigned int flags,
                                     const char* label)
{
    if (!cache || gl_texture == 0 || width <= 0 || height <= 0) return -1;
    if (cache->resource
        && cache->texture == gl_texture
        && cache->width == width
        && cache->height == height
        && cache->flags == flags) {
        return 0;
    }
    const int reset_rc = reset_cached_gl_resource(cache, 0);
    if (reset_rc != 0) return reset_rc;
    cudaGraphicsResource* resource = NULL;
    cudaError_t e = cudaGraphicsGLRegisterImage(&resource,
                                                gl_texture,
                                                GL_TEXTURE_2D,
                                                flags);
    if (e != cudaSuccess) {
        fprintf(stderr,
                "[igpu_recon_cuda] cudaGraphicsGLRegisterImage(%s) failed: %s\n",
                label ? label : "cached GL texture",
                cudaGetErrorString(e));
        return 2;
    }
    cache->resource = resource;
    cache->texture = gl_texture;
    cache->width = width;
    cache->height = height;
    cache->flags = flags;
    return 0;
}

static int copy_bayer16_to_gl_r16_texture(igpu_recon_backend* b, unsigned int gl_texture)
{
    if (!b || gl_texture == 0) return -1;

    const unsigned int flags = cudaGraphicsRegisterFlagsWriteDiscard;
    const int cache_rc = ensure_cached_gl_resource(&b->gl_r16_resource,
                                                   gl_texture,
                                                   b->width,
                                                   b->height,
                                                   flags,
                                                   "GL_R16 texture");
    if (cache_rc != 0) return cache_rc;

    cudaGraphicsResource* resource = b->gl_r16_resource.resource;
    cudaError_t e = cudaGraphicsMapResources(1, &resource, 0);

    if (e != cudaSuccess) {
        fprintf(stderr,
                "[igpu_recon_cuda] cudaGraphicsMapResources(GL_R16 texture) failed: %s\n",
                cudaGetErrorString(e));
        reset_cached_gl_resource(&b->gl_r16_resource, 0);
        return 2;
    }

    cudaArray_t mapped_array = NULL;
    e = cudaGraphicsSubResourceGetMappedArray(&mapped_array, resource, 0, 0);
    if (e == cudaSuccess) {
        const size_t row_bytes = (size_t)b->width * sizeof(uint16_t);
        e = cudaMemcpy2DToArray(mapped_array,
                                0,
                                0,
                                b->d_out,
                                row_bytes,
                                row_bytes,
                                (size_t)b->height,
                                cudaMemcpyDeviceToDevice);
    }

    const cudaError_t unmap_error = cudaGraphicsUnmapResources(1, &resource, 0);
    if (e != cudaSuccess) {
        fprintf(stderr,
                "[igpu_recon_cuda] device-to-GL texture copy failed: %s\n",
                cudaGetErrorString(e));
        reset_cached_gl_resource(&b->gl_r16_resource, 0);
        return 2;
    }
    if (unmap_error != cudaSuccess) {
        fprintf(stderr,
                "[igpu_recon_cuda] cudaGraphicsUnmapResources(GL_R16 texture) failed: %s\n",
                cudaGetErrorString(unmap_error));
        reset_cached_gl_resource(&b->gl_r16_resource, 0);
        return 2;
    }

    return 0;
}

static void free_clip_buffers(igpu_recon_backend* b) {
    if (!b) return;
    if (b->d_in)       cudaFree(b->d_in);
    if (b->d_out)      cudaFree(b->d_out);
    if (b->d_alias)    cudaFree(b->d_alias);
    if (b->d_aliasaux) cudaFree(b->d_aliasaux);
    if (b->d_over)     cudaFree(b->d_over);
    if (b->d_overaux)  cudaFree(b->d_overaux);
    if (b->d_raw32)    cudaFree(b->d_raw32);
    if (b->d_dark)     cudaFree(b->d_dark);
    if (b->d_bright)   cudaFree(b->d_bright);
    if (b->d_fullres)  cudaFree(b->d_fullres);
    if (b->d_halfres)  cudaFree(b->d_halfres);
    if (b->d_fullres_smooth) cudaFree(b->d_fullres_smooth);
    if (b->d_halfres_smooth) cudaFree(b->d_halfres_smooth);
    if (b->d_chroma_tmp) cudaFree(b->d_chroma_tmp);
    b->d_in=b->d_out=b->d_alias=b->d_aliasaux=b->d_over=b->d_overaux=NULL;
    b->d_raw32=b->d_dark=b->d_bright=b->d_fullres=b->d_halfres=NULL;
    b->d_fullres_smooth=b->d_halfres_smooth=NULL;
    b->d_chroma_tmp=NULL;
    b->clip_allocated_bytes = 0;
    b->chroma_tmp_allocated_bytes = 0;
}

static void reset_preupload_slots(igpu_recon_backend* b, int release_storage)
{
    if (!b) return;
    if (b->preupload_stream) cudaStreamSynchronize(b->preupload_stream);
    if (b->preupload_lock_initialized) EnterCriticalSection(&b->preupload_lock);
    for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
        PreuploadSlot* slot = &b->preupload[i];
        slot->state = 0;
        slot->frame_id = 0;
        slot->sequence = 0;
        slot->submitted_while_prior_run_active = 0;
        slot->host_staging_ms = 0.0;
        if (release_storage) {
            if (slot->host_input) cudaFreeHost(slot->host_input);
            if (slot->device_input) cudaFree(slot->device_input);
            slot->host_input = NULL;
            slot->device_input = NULL;
            slot->bytes = 0;
        }
    }
    if (release_storage) b->preupload_device_allocated_bytes = 0;
    memset(&b->last_preupload_status, 0, sizeof(b->last_preupload_status));
    if (b->preupload_lock_initialized) LeaveCriticalSection(&b->preupload_lock);
}

static int ensure_preupload_slot_storage(igpu_recon_backend* b,
                                         PreuploadSlot* slot,
                                         size_t bytes)
{
    if (!b || !slot || bytes == 0) return -1;
    if (slot->host_input && slot->device_input && slot->bytes == bytes) return 0;
    if (slot->host_input) cudaFreeHost(slot->host_input);
    if (slot->device_input) {
        cudaFree(slot->device_input);
        if (b->preupload_device_allocated_bytes >= slot->bytes) {
            b->preupload_device_allocated_bytes -= (uint64_t)slot->bytes;
        }
    }
    slot->host_input = NULL;
    slot->device_input = NULL;
    slot->bytes = 0;
    cudaError_t e = cudaMallocHost((void**)&slot->host_input, bytes);
    if (e != cudaSuccess) return -1;
    e = cudaMalloc((void**)&slot->device_input, bytes);
    if (e != cudaSuccess) {
        cudaFreeHost(slot->host_input);
        slot->host_input = NULL;
        return -1;
    }
    slot->bytes = bytes;
    b->preupload_device_allocated_bytes += (uint64_t)bytes;
    return 0;
}

static void free_retained_device_outputs(igpu_recon_backend* b) {
    if (!b) return;
    for (int i = 0; i < RETAINED_DEVICE_OUTPUT_SLOTS; ++i) {
        if (b->retained_device_output[i]) {
            cudaFree(b->retained_device_output[i]);
        }
        b->retained_device_output[i] = NULL;
        b->retained_device_output_token[i] = 0;
        b->retained_device_output_in_use[i] = 0;
        b->retained_device_output_width[i] = 0;
        b->retained_device_output_height[i] = 0;
    }
    b->retained_device_output_allocated_bytes = 0;
}

static void free_lut_buffers(igpu_recon_backend* b) {
    if (!b) return;
    if (b->dd_raw2ev) cudaFree(b->dd_raw2ev);
    if (b->dd_ev2raw) cudaFree(b->dd_ev2raw);
    if (b->dd_mix)    cudaFree(b->dd_mix);
    if (b->dd_frc_f)  cudaFree(b->dd_frc_f);
    if (b->dd_frc_d)  cudaFree(b->dd_frc_d);
    if (b->df_randn05) cudaFree(b->df_randn05);
    b->dd_raw2ev=b->dd_ev2raw=NULL;
    b->dd_mix=b->dd_frc_f=b->dd_frc_d=NULL;
    b->df_randn05=NULL;
    b->lut_allocated_bytes = 0;
}

extern "C" {

IGPU_API
igpu_recon_backend* igpu_recon_create(const char* backend_name)
{
    if (backend_name && strcmp(backend_name, "cuda") != 0) {
        fprintf(stderr, "[igpu_recon_cuda] unsupported backend '%s' (this DLL is cuda)\n",
                backend_name);
        return NULL;
    }
    int devcount = 0;
    cudaError_t e = cudaGetDeviceCount(&devcount);
    if (e != cudaSuccess || devcount < 1) {
        fprintf(stderr, "[igpu_recon_cuda] no CUDA device: %s\n",
                cudaGetErrorString(e));
        return NULL;
    }
    int dev = 0;
    CKN(cudaSetDevice(dev));
    cudaDeviceProp prop;
    CKN(cudaGetDeviceProperties(&prop, dev));

    igpu_recon_backend* b = (igpu_recon_backend*)calloc(1, sizeof(igpu_recon_backend));
    if (!b) return NULL;
    b->device = dev;
    b->next_retained_device_output_token = 1;
    b->next_preupload_sequence = 1;
    snprintf(b->describe, sizeof(b->describe), "CUDA / %s", prop.name);

    InitializeCriticalSection(&b->preupload_lock);
    b->preupload_lock_initialized = 1;

    /* timing events */
    if (cudaEventCreate(&b->ev_start)        != cudaSuccess ||
        cudaEventCreate(&b->ev_after_up)     != cudaSuccess ||
        cudaEventCreate(&b->ev_after_kernel) != cudaSuccess ||
        cudaEventCreate(&b->ev_after_dl)     != cudaSuccess) {
        fprintf(stderr, "[igpu_recon_cuda] cudaEventCreate failed\n");
        free(b);
        return NULL;
    }
    return b;
}

IGPU_API
void igpu_recon_destroy(igpu_recon_backend* b)
{
    if (!b) return;
    cudaSetDevice(b->device);
    reset_preupload_slots(b, 1);
    for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
        if (b->preupload[i].upload_start)
            cudaEventDestroy(b->preupload[i].upload_start);
        if (b->preupload[i].upload_ready)
            cudaEventDestroy(b->preupload[i].upload_ready);
    }
    if (b->preupload_stream) cudaStreamDestroy(b->preupload_stream);
    if (b->preupload_lock_initialized) {
        DeleteCriticalSection(&b->preupload_lock);
        b->preupload_lock_initialized = 0;
    }
    reset_cached_gl_resource(&b->gl_r16_resource, 1);
    free_retained_device_outputs(b);
    free_clip_buffers(b);
    free_lut_buffers(b);
    if (b->ev_start)        cudaEventDestroy(b->ev_start);
    if (b->ev_after_up)     cudaEventDestroy(b->ev_after_up);
    if (b->ev_after_kernel) cudaEventDestroy(b->ev_after_kernel);
    if (b->ev_after_dl)     cudaEventDestroy(b->ev_after_dl);
    free(b);
}

IGPU_API
int igpu_recon_reset_gl_texture_resources(igpu_recon_backend* b)
{
    if (!b) return -1;
    const cudaError_t e = cudaSetDevice(b->device);
    if (e != cudaSuccess) {
        fprintf(stderr,
                "[igpu_recon_cuda] cudaSetDevice(reset GL texture resources) failed: %s\n",
                cudaGetErrorString(e));
        return 2;
    }
    return reset_cached_gl_resource(&b->gl_r16_resource, 1);
}

IGPU_API
int igpu_recon_abi_version(igpu_recon_backend* b)
{
    (void)b;
    return IGPU_RECON_ABI_VERSION;
}

IGPU_API
const char* igpu_recon_describe(igpu_recon_backend* b)
{
    if (!b) return "CUDA / (no backend)";
    return b->describe;
}

IGPU_API
int igpu_recon_set_clip(igpu_recon_backend* b, const igpu_recon_clip_t* clip)
{
    if (!b || !clip) return -1;
    if (clip->width <= 0 || clip->height <= 0) return -1;

    CK(cudaSetDevice(b->device));

    /* (re)allocate device buffers sized to the clip */
    reset_preupload_slots(b, 1);
    free_retained_device_outputs(b);
    free_clip_buffers(b);
    b->width  = clip->width;
    b->height = clip->height;
    b->black  = clip->black_level;
    b->white  = clip->white_level;
    for (int i=0;i<4;i++) b->is_bright[i] = clip->is_bright[i];

    const size_t n = (size_t)b->width * (size_t)b->height;
    CK(cudaMalloc(&b->d_in,      n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_out,     n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_alias,   n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_aliasaux,n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_over,    n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_overaux, n*sizeof(uint16_t)));
    CK(cudaMalloc(&b->d_raw32,   n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_dark,    n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_bright,  n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_fullres, n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_halfres, n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_fullres_smooth, n*sizeof(uint32_t)));
    CK(cudaMalloc(&b->d_halfres_smooth, n*sizeof(uint32_t)));
    b->clip_allocated_bytes =
        (uint64_t)n * (6ull * (uint64_t)sizeof(uint16_t)
                     + 7ull * (uint64_t)sizeof(uint32_t));

    /* push the static per-clip constants to __constant__.
     * white_darkened is recomputed in set_clip exactly as the engine does,
     * so the validated parity constant (174632 for the oracle config) is
     * REPRODUCED from the levels rather than hard-coded:
     *   dualiso.c:4828-4833  white_bright = ((white20/64)/2)*64
     *   dualiso.c:2465,2548  white = MIN(white20, white_bright);
     *                        white_darkened = (white - black + bd)*factor + black
     * with bd=0 and factor=0.125 (the 3-EV / 8x ISO ratio path).
     * Check: black20=131008 white20=960000 ->
     *   white_bright = ((960000/64)/2)*64 = 480000
     *   white = MIN(960000,480000) = 480000
     *   white_darkened = (480000-131008)*0.125 + 131008 = 174632  (matches oracle) */
    int black20 = b->black;
    int white20 = b->white;
    int white_bright = ((white20 / 64) / 2) * 64;
    int wmin = (white20 < white_bright) ? white20 : white_bright;
    int black_delta20 = 0;
    int white_darkened = (int)(((double)(wmin - black20 + black_delta20)) * HOST_FACTOR) + black20;
    /* dark_noise default 8 (14-bit) * 64 = 512 in 20-bit; v1 fixed. */
    int dark_noise20 = 512;
    double factor = HOST_FACTOR;

    CK(cudaMemcpyToSymbol(d_black,          &black20,        sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white,          &white20,        sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white_darkened, &white_darkened, sizeof(int)));
    CK(cudaMemcpyToSymbol(d_dark_noise,     &dark_noise20,   sizeof(int)));
    CK(cudaMemcpyToSymbol(d_factor,         &factor,         sizeof(double)));
    CK(cudaMemcpyToSymbol(d_black_delta20,  &black_delta20,  sizeof(int)));
    CK(cudaMemcpyToSymbol(d_is_bright,      b->is_bright,    sizeof(int)*4));

    b->have_clip = 1;
    return 0;
}

IGPU_API
int igpu_recon_set_luts(igpu_recon_backend* b, const igpu_recon_luts_t* luts)
{
    if (!b || !luts) return -1;
    CK(cudaSetDevice(b->device));

    /* allocate LUT buffers if not already present */
    if (!b->dd_raw2ev) CK(cudaMalloc(&b->dd_raw2ev, (size_t)RAW2EV_COUNT*sizeof(int)));
    if (!b->dd_ev2raw) CK(cudaMalloc(&b->dd_ev2raw, (size_t)EV2RAW_COUNT*sizeof(int)));
    if (!b->dd_mix)    CK(cudaMalloc(&b->dd_mix,    (size_t)RAW2EV_COUNT*sizeof(double)));
    if (!b->dd_frc_f)  CK(cudaMalloc(&b->dd_frc_f,  (size_t)RAW2EV_COUNT*sizeof(double)));
    if (!b->dd_frc_d)  CK(cudaMalloc(&b->dd_frc_d,  (size_t)RAW2EV_COUNT*sizeof(double)));
    b->lut_allocated_bytes =
        (uint64_t)RAW2EV_COUNT * ((uint64_t)sizeof(int) + 3ull * (uint64_t)sizeof(double))
      + (uint64_t)EV2RAW_COUNT * (uint64_t)sizeof(int);

    /* NULL pointer means "unchanged since last set" per the ABI contract. */
    if (luts->raw2ev)
        CK(cudaMemcpy(b->dd_raw2ev, luts->raw2ev, (size_t)RAW2EV_COUNT*sizeof(int),  cudaMemcpyHostToDevice));
    if (luts->ev2raw)
        CK(cudaMemcpy(b->dd_ev2raw, luts->ev2raw, (size_t)EV2RAW_COUNT*sizeof(int),  cudaMemcpyHostToDevice));
    if (luts->mix_curve)
        CK(cudaMemcpy(b->dd_mix,    luts->mix_curve, (size_t)RAW2EV_COUNT*sizeof(double), cudaMemcpyHostToDevice));
    if (luts->fullres_curve) {
        /* the ABI exposes ONE fullres curve. final_blend uses the float-cast
         * curve; alias_map uses the double curve. fullres_curve.f64 was built
         * as (double)(float)f, so the float-cast and double values coincide to
         * within the float<->double roundtrip the kernels already perform.
         * Upload the same host curve to both device slots. */
        CK(cudaMemcpy(b->dd_frc_f, luts->fullres_curve, (size_t)RAW2EV_COUNT*sizeof(double), cudaMemcpyHostToDevice));
        CK(cudaMemcpy(b->dd_frc_d, luts->fullres_curve, (size_t)RAW2EV_COUNT*sizeof(double), cudaMemcpyHostToDevice));
    }
    if (luts->randn05) {
        if (!b->df_randn05) CK(cudaMalloc(&b->df_randn05, (size_t)1024*sizeof(float)));
        CK(cudaMemcpy(b->df_randn05, luts->randn05, (size_t)1024*sizeof(float), cudaMemcpyHostToDevice));
    }
    if (b->df_randn05) {
        b->lut_allocated_bytes += 1024ull * (uint64_t)sizeof(float);
    }

    b->have_luts = 1;
    return 0;
}

static int ensure_preupload_runtime(igpu_recon_backend* b)
{
    if (!b) return -1;
    EnterCriticalSection(&b->preupload_lock);
    if (b->preupload_stream) {
        LeaveCriticalSection(&b->preupload_lock);
        return 0;
    }
    cudaError_t e = cudaStreamCreateWithFlags(&b->preupload_stream,
                                               cudaStreamNonBlocking);
    for (int i = 0; e == cudaSuccess && i < PREUPLOAD_SLOTS; ++i) {
        e = cudaEventCreate(&b->preupload[i].upload_start);
        if (e == cudaSuccess) {
            e = cudaEventCreate(&b->preupload[i].upload_ready);
        }
    }
    if (e != cudaSuccess) {
        for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
            if (b->preupload[i].upload_start)
                cudaEventDestroy(b->preupload[i].upload_start);
            if (b->preupload[i].upload_ready)
                cudaEventDestroy(b->preupload[i].upload_ready);
            b->preupload[i].upload_start = NULL;
            b->preupload[i].upload_ready = NULL;
        }
        if (b->preupload_stream) cudaStreamDestroy(b->preupload_stream);
        b->preupload_stream = NULL;
    }
    LeaveCriticalSection(&b->preupload_lock);
    return e == cudaSuccess ? 0 : -1;
}

IGPU_API
int igpu_recon_preupload_frame(igpu_recon_backend* b,
                               uint64_t frame_id,
                               const uint16_t* in_bayer14,
                               size_t input_bytes,
                               int submitted_while_prior_run_active)
{
    if (!b || !in_bayer14 || !b->have_clip) return -1;
    const size_t expected_bytes =
        (size_t)b->width * (size_t)b->height * sizeof(uint16_t);
    if (input_bytes != expected_bytes) return -1;
    if (cudaSetDevice(b->device) != cudaSuccess) return -1;
    if (ensure_preupload_runtime(b) != 0) return -1;

    int selected = -1;
    uint64_t oldest_sequence = UINT64_MAX;
    EnterCriticalSection(&b->preupload_lock);
    for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
        if (b->preupload[i].state == 0) {
            selected = i;
            break;
        }
    }
    if (selected < 0) {
        /* Seeks and cancelled lookahead can leave completed frames queued.
         * Reclaim only completed queued slots; staging/in-use slots are never
         * touched.  Oldest-first keeps the newest lookahead candidates. */
        for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
            PreuploadSlot* candidate = &b->preupload[i];
            if (candidate->state == 1 &&
                cudaEventQuery(candidate->upload_ready) == cudaSuccess &&
                candidate->sequence < oldest_sequence) {
                oldest_sequence = candidate->sequence;
                selected = i;
            }
        }
    }
    if (selected >= 0) b->preupload[selected].state = 3;
    LeaveCriticalSection(&b->preupload_lock);
    if (selected < 0) return 4;

    PreuploadSlot* slot = &b->preupload[selected];
    if (ensure_preupload_slot_storage(b, slot, input_bytes) != 0) {
        EnterCriticalSection(&b->preupload_lock);
        slot->state = 0;
        LeaveCriticalSection(&b->preupload_lock);
        return -1;
    }

    const double staging_start_ms = qpc_now_ms();
    memcpy(slot->host_input, in_bayer14, input_bytes);
    const double host_staging_ms = qpc_now_ms() - staging_start_ms;
    cudaError_t e = cudaEventRecord(slot->upload_start, b->preupload_stream);
    if (e == cudaSuccess) {
        e = cudaMemcpyAsync(slot->device_input,
                            slot->host_input,
                            input_bytes,
                            cudaMemcpyHostToDevice,
                            b->preupload_stream);
    }
    if (e == cudaSuccess) {
        e = cudaEventRecord(slot->upload_ready, b->preupload_stream);
    }
    EnterCriticalSection(&b->preupload_lock);
    if (e == cudaSuccess) {
        slot->bytes = input_bytes;
        slot->frame_id = frame_id;
        slot->sequence = b->next_preupload_sequence++;
        slot->submitted_while_prior_run_active =
            submitted_while_prior_run_active != 0;
        slot->host_staging_ms = host_staging_ms;
        slot->state = 1;
    } else {
        slot->state = 0;
    }
    LeaveCriticalSection(&b->preupload_lock);
    return e == cudaSuccess ? 0 : -1;
}

static int take_exact_preupload(igpu_recon_backend* b,
                                uint64_t frame_id,
                                const uint16_t* in_bayer14,
                                size_t input_bytes,
                                const uint16_t** device_input,
                                int* slot_index,
                                igpu_recon_preupload_status_t* status)
{
    if (!b || !in_bayer14 || !device_input || !slot_index || !status) return 0;
    memset(status, 0, sizeof(*status));
    *device_input = NULL;
    *slot_index = -1;

    EnterCriticalSection(&b->preupload_lock);
    for (int i = 0; i < PREUPLOAD_SLOTS; ++i) {
        if (b->preupload[i].state == 1 &&
            b->preupload[i].frame_id == frame_id) {
            b->preupload[i].state = 2;
            *slot_index = i;
            status->accepted = 1;
            status->submitted_while_prior_run_active =
                b->preupload[i].submitted_while_prior_run_active;
            status->host_staging_ms = b->preupload[i].host_staging_ms;
            break;
        }
    }
    LeaveCriticalSection(&b->preupload_lock);
    if (*slot_index < 0) return 0;

    PreuploadSlot* slot = &b->preupload[*slot_index];
    const cudaError_t query = cudaEventQuery(slot->upload_ready);
    status->ready_before_run = query == cudaSuccess;
    const double wait_start_ms = qpc_now_ms();
    const cudaError_t sync = cudaEventSynchronize(slot->upload_ready);
    status->upload_wait_ms = qpc_now_ms() - wait_start_ms;
    if (sync == cudaSuccess) {
        float upload_ms = 0.0f;
        if (cudaEventElapsedTime(&upload_ms,
                                 slot->upload_start,
                                 slot->upload_ready) == cudaSuccess) {
            status->upload_ms = (double)upload_ms;
        }
    }
    status->exact_match =
        sync == cudaSuccess &&
        slot->bytes == input_bytes &&
        memcmp(slot->host_input, in_bayer14, input_bytes) == 0;
    if (status->exact_match) {
        status->used = 1;
        *device_input = slot->device_input;
        return 1;
    }

    EnterCriticalSection(&b->preupload_lock);
    slot->state = 0;
    LeaveCriticalSection(&b->preupload_lock);
    *slot_index = -1;
    return 0;
}

static int igpu_recon_run_internal(igpu_recon_backend* b,
                                   uint64_t frame_id,
                                   int allow_preupload,
                                   const igpu_recon_frame_t* frame,
                                   const uint16_t* in_bayer14,
                                   igpu_recon_out_kind out_kind,
                                   uint16_t* out_bayer16,
                                   unsigned int gl_texture)
{
    (void)gl_texture;
    if (!b || !frame || !in_bayer14) return -1;
    if (!b->have_clip || !b->have_luts) {
        fprintf(stderr, "[igpu_recon_cuda] run() before set_clip/set_luts\n");
        return -1;
    }
    if (frame->interp_method != 1 ||
        (frame->use_alias_map != 0 && frame->use_alias_map != 1) ||
        !frame->use_fullres ||
        frame->chroma_smooth_method < 0 ||
        frame->chroma_smooth_method > 1 ||
        frame->playback_preview_scale_factor < 0 ||
        (frame->apply_dither != 0 && frame->apply_dither != 1)) {
        fprintf(stderr, "[igpu_recon_cuda] unsupported frame controls "
                        "(interp=%d alias=%d fullres=%d chroma=%d scale=%d dither=%d)\n",
                frame->interp_method,
                frame->use_alias_map,
                frame->use_fullres,
                frame->chroma_smooth_method,
                frame->playback_preview_scale_factor,
                frame->apply_dither);
        return 3;
    }
    if (frame->apply_dither && !b->df_randn05) {
        fprintf(stderr, "[igpu_recon_cuda] dither requested without randn05 LUT\n");
        return 3;
    }
    const int output_to_gl_texture = (out_kind == IGPU_OUT_GL_TEXTURE);
    const int output_to_device_bayer16 = (out_kind == IGPU_OUT_DEVICE_BAYER16);
    if (output_to_gl_texture) {
        if (gl_texture == 0) return -1;
    } else if (out_kind == IGPU_OUT_CPU16) {
        if (!out_bayer16) return -1;
    } else if (!output_to_device_bayer16) {
        return -1;
    }

    CK(cudaSetDevice(b->device));

    const int W = b->width, H = b->height;
    const size_t n = (size_t)W * (size_t)H;
    const size_t input_bytes = n * sizeof(uint16_t);
    int* dd_ev2raw_origin = b->dd_ev2raw + EV2RAW_ORIGIN;
    const uint16_t* kernel_input = NULL;
    int preupload_slot_index = -1;
    igpu_recon_preupload_status_t preupload_status;
    memset(&preupload_status, 0, sizeof(preupload_status));

    int black20 = b->black;
    int white20 = b->white;
    int white_darkened = frame->white_darkened;
    int black_delta20 = frame->black_delta;
    int dark_noise20 = (int)(frame->dark_noise + 0.5);
    double corr_ev = fabs(frame->ev_correction);
    double factor = pow(2.0, -corr_ev);

    if (white_darkened <= 0 || dark_noise20 <= 0 || corr_ev < 0.5) {
        fprintf(stderr, "[igpu_recon_cuda] invalid frame constants "
                        "(white_darkened=%d dark_noise=%d corr_ev=%.4f)\n",
                white_darkened, dark_noise20, corr_ev);
        return 3;
    }

    CK(cudaMemcpyToSymbol(d_black,          &black20,        sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white,          &white20,        sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white_darkened, &white_darkened, sizeof(int)));
    CK(cudaMemcpyToSymbol(d_dark_noise,     &dark_noise20,   sizeof(int)));
    CK(cudaMemcpyToSymbol(d_factor,         &factor,         sizeof(double)));
    CK(cudaMemcpyToSymbol(d_black_delta20,  &black_delta20,  sizeof(int)));
    CK(cudaMemcpyToSymbol(d_is_bright,      b->is_bright,    sizeof(int)*4));

    dim3 bt(16,16);
    dim3 gt((W+15)/16,(H+15)/16);

    /* scratch buffers that the recon pre-zeroes */
    CK(cudaMemset(b->d_alias,   0, n*sizeof(uint16_t)));
    CK(cudaMemset(b->d_dark,    0, n*sizeof(uint32_t)));
    CK(cudaMemset(b->d_bright,  0, n*sizeof(uint32_t)));
    CK(cudaMemset(b->d_fullres, 0, n*sizeof(uint32_t)));
    CK(cudaMemset(b->d_halfres, 0, n*sizeof(uint32_t)));
    CK(cudaMemset(b->d_over,    0, n*sizeof(uint16_t)));
    CK(cudaMemset(b->d_overaux, 0, n*sizeof(uint16_t)));

    /* ---- timed: H2D upload (14-bit Bayer) ---- */
    if (allow_preupload) {
        (void)take_exact_preupload(b,
                                   frame_id,
                                   in_bayer14,
                                   input_bytes,
                                   &kernel_input,
                                   &preupload_slot_index,
                                   &preupload_status);
    }
    CK(cudaEventRecord(b->ev_start, 0));
    if (!kernel_input) {
        CK(cudaMemcpy(b->d_in,
                      in_bayer14,
                      input_bytes,
                      cudaMemcpyHostToDevice));
        kernel_input = b->d_in;
    }
    CK(cudaEventRecord(b->ev_after_up, 0));

    /* ---- timed: kernel chain ---- */
    /* STAGE 1+2 */
    k_promote_match<<<gt,bt>>>(kernel_input, b->d_raw32, W, H);

    /* STAGE 3: mean23 over even-x indices */
    {
        int nx = (W-3-2+1)/2 + 1;
        dim3 g((nx+15)/16,(H+15)/16);
        k_mean23<<<g,bt>>>(b->d_raw32, b->d_dark, b->d_bright,
                           b->dd_raw2ev, dd_ev2raw_origin, W, H);
    }
    /* STAGE 4: border (top, bottom, then cols - scalar order) */
    { dim3 g((W+15)/16,(3+15)/16); k_border_toprows<<<g,bt>>>(b->d_raw32,b->d_dark,b->d_bright,W,H); }
    { dim3 g((W+15)/16,(4+15)/16); k_border_botrows<<<g,bt>>>(b->d_raw32,b->d_dark,b->d_bright,W,H); }
    { int t=256; dim3 g((H+t-1)/t); k_border_cols<<<g,t>>>(b->d_raw32,b->d_dark,b->d_bright,W,H); }

    const int apply_chroma_smooth = (frame->chroma_smooth_method == 1);
    if (apply_chroma_smooth && !b->d_chroma_tmp) {
        CK(cudaMalloc(&b->d_chroma_tmp, n*sizeof(uint32_t)));
        b->chroma_tmp_allocated_bytes = (uint64_t)n * (uint64_t)sizeof(uint32_t);
        b->clip_allocated_bytes += b->chroma_tmp_allocated_bytes;
    }

    const uint32_t* dark_for_mix = b->d_dark;
    const uint32_t* bright_for_mix = b->d_bright;
    if (apply_chroma_smooth) {
        dim3 gcs(((W / 2) + 15) / 16, ((H / 2) + 15) / 16);
        CK(cudaMemcpy(b->d_fullres_smooth,b->d_bright,n*sizeof(uint32_t),cudaMemcpyDeviceToDevice));
        CK(cudaMemcpy(b->d_halfres_smooth,b->d_dark,n*sizeof(uint32_t),cudaMemcpyDeviceToDevice));
        k_chroma_smooth_2x2<<<gcs,bt>>>(b->d_bright,b->d_fullres_smooth,
                                        b->dd_raw2ev,dd_ev2raw_origin,white_darkened,W,H);
        k_chroma_smooth_2x2<<<gcs,bt>>>(b->d_dark,b->d_halfres_smooth,
                                        b->dd_raw2ev,dd_ev2raw_origin,white20,W,H);
        bright_for_mix = b->d_fullres_smooth;
        dark_for_mix = b->d_halfres_smooth;
    }

    /* STAGE 5: fullres. CPU chroma smoothing keeps the primary fullres
     * reconstruction on the original matched planes; smoothed source planes
     * feed the halfres mix and secondary fullres_smooth reconstruction. */
    if (apply_chroma_smooth) {
        k_fullres_phase_balance<<<gt,bt>>>(b->d_fullres,b->d_dark,b->d_bright,
                                           b->dd_raw2ev,dd_ev2raw_origin,W,H);
    } else {
        k_fullres<<<gt,bt>>>(b->d_fullres,b->d_dark,b->d_bright,W,H);
    }

    /* STAGE 6: mix halfres */
    if (frame->apply_dither) {
        k_mix_halfres_avx2<<<gt,bt>>>(b->d_halfres,bright_for_mix,dark_for_mix,
                                      b->dd_raw2ev,dd_ev2raw_origin,b->dd_mix,W,H);
    } else {
        k_mix_halfres<<<gt,bt>>>(b->d_halfres,bright_for_mix,dark_for_mix,
                                 b->dd_raw2ev,dd_ev2raw_origin,b->dd_mix,W,H);
    }

    uint32_t* fullres_for_final = b->d_fullres;
    uint32_t* fullres_smooth = b->d_fullres;
    uint32_t* halfres_smooth = b->d_halfres;
    if (apply_chroma_smooth) {
        /* CPU diso_get_full20bit calls mix_images(..., chroma_smooth_method=0)
         * after source-plane smoothing. The only post-mix chroma input is the
         * secondary fullres reconstruction from the smoothed source planes. */
        k_fullres<<<gt,bt>>>(b->d_chroma_tmp,dark_for_mix,bright_for_mix,W,H);
        fullres_smooth = b->d_chroma_tmp;
    }

    /* STAGE 7: alias map. Alias-off CPU recon blends with c=0; d_alias is
     * already zeroed above, so skip the alias kernels for that mode. */
    if (frame->use_alias_map) {
        /* CPU mix_images receives dark_for_mix/bright_for_mix. With source chroma
         * smoothing enabled, the alias-map fullres-threshold gates are therefore
         * keyed from the smoothed bright plane, while final_blend still uses the
         * original bright/dark planes below. */
        k_alias_init<<<gt,bt>>>(b->d_alias,fullres_for_final,halfres_smooth,bright_for_mix,
                                b->dd_raw2ev,b->dd_frc_d,W,H);
        CK(cudaMemcpy(b->d_aliasaux,b->d_alias,n*sizeof(uint16_t),cudaMemcpyDeviceToDevice));
        { dim3 g((W-12+15)/16,(H-12+15)/16); k_alias_rank<<<g,bt>>>(b->d_alias,b->d_aliasaux,bright_for_mix,b->dd_frc_d,W,H); }
        { dim3 g((W-12+15)/16,(H-12+15)/16); k_alias_gauss<<<g,bt>>>(b->d_alias,b->d_aliasaux,bright_for_mix,b->dd_frc_d,W,H); }
        { int nx=(W-2-2)/2+1, ny=(H-2-2)/2+1; dim3 g((nx+15)/16,(ny+15)/16); k_alias_gray<<<g,bt>>>(b->d_alias,W,H); }
    }

    /* STAGE 8: overexposed. CPU mix_images marks from the same dark/bright
     * planes passed into the halfres mix; with source chroma smoothing those
     * are the smoothed source planes, not the original split planes. */
    k_over_mark<<<gt,bt>>>(b->d_overaux,bright_for_mix,dark_for_mix,W,H);
    k_over_bordercopy<<<gt,bt>>>(b->d_over,b->d_overaux,W,H);
    { dim3 g((W-6+15)/16,(H-6+15)/16); k_over_blur<<<g,bt>>>(b->d_over,b->d_overaux,W,H); }

    const int apply_fullres_mesh_guard =
        frame->playback_preview_scale_factor == 1 &&
        frame->use_fullres &&
        !frame->use_alias_map &&
        apply_chroma_smooth;

    if (apply_fullres_mesh_guard) {
        k_final_blend_mesh_guard<<<gt,bt>>>(b->d_raw32,b->d_bright,halfres_smooth,
                                            fullres_for_final,fullres_smooth,
                                            b->d_dark,b->d_over,b->d_alias,
                                            b->dd_raw2ev,dd_ev2raw_origin,b->dd_frc_f,W,H);
        CK(cudaMemcpy(b->d_fullres,b->d_raw32,n*sizeof(uint32_t),cudaMemcpyDeviceToDevice));
        k_output_vertical_mesh_stabilize<<<gt,bt>>>(b->d_raw32,b->d_fullres,
                                                    b->dd_raw2ev,dd_ev2raw_origin,W,H);
        CK(cudaMemcpy(b->d_fullres,b->d_raw32,n*sizeof(uint32_t),cudaMemcpyDeviceToDevice));
        k_source_mesh_stabilize<<<gt,bt>>>(b->d_raw32,b->d_fullres,
                                           b->dd_raw2ev,dd_ev2raw_origin,W,H);
        k_flat_green_cell_balance<<<gt,bt>>>(b->d_raw32,b->dd_raw2ev,dd_ev2raw_origin,W,H);
        k_flat_chroma_guard<<<gt,bt>>>(b->d_raw32,b->dd_raw2ev,dd_ev2raw_origin,W,H);
        k_convert16<<<gt,bt>>>(b->d_out,b->d_raw32,W,H);
    } else if (frame->apply_dither) {
        /* STAGE 9+10: default AVX2 export shape, fused to 16-bit with row dither. */
        k_final_blend16_avx2<<<gt,bt>>>(b->d_out,b->d_bright,halfres_smooth,fullres_for_final,fullres_smooth,
                                        b->d_dark,b->d_over,b->d_alias,
                                        b->dd_raw2ev,dd_ev2raw_origin,b->dd_frc_f,b->df_randn05,W,H);
    } else {
        /* STAGE 9: scalar export shape, final blend to raw32. */
        k_final_blend<<<gt,bt>>>(b->d_raw32,b->d_bright,halfres_smooth,fullres_for_final,fullres_smooth,
                                 b->d_dark,b->d_over,b->d_alias,
                                 b->dd_raw2ev,dd_ev2raw_origin,b->dd_frc_f,W,H);

        /* STAGE 10: convert 20->16 without AVX2 row dither. */
        k_convert16<<<gt,bt>>>(b->d_out,b->d_raw32,W,H);
    }

    debug_dump_recon_stages(b, frame, fullres_smooth, halfres_smooth);

    CK(cudaGetLastError());
    CK(cudaEventRecord(b->ev_after_kernel, 0));

    /* ---- timed: D2H download or CUDA->GL interop handoff ---- */
    if (output_to_gl_texture) {
        const int rc = copy_bayer16_to_gl_r16_texture(b, gl_texture);
        if (rc != 0) return rc;
    } else if (out_kind == IGPU_OUT_CPU16) {
        CK(cudaMemcpy(out_bayer16, b->d_out, n*sizeof(uint16_t), cudaMemcpyDeviceToHost));
    }
    CK(cudaEventRecord(b->ev_after_dl, 0));
    CK(cudaEventSynchronize(b->ev_after_dl));

    /* collect timings */
    float up=0, kern=0, dl=0, tot=0;
    CK(cudaEventElapsedTime(&up,   b->ev_start,        b->ev_after_up));
    CK(cudaEventElapsedTime(&kern, b->ev_after_up,     b->ev_after_kernel));
    CK(cudaEventElapsedTime(&dl,   b->ev_after_kernel, b->ev_after_dl));
    CK(cudaEventElapsedTime(&tot,  b->ev_start,        b->ev_after_dl));
    b->last_timing.upload_ms   = (double)up;
    b->last_timing.match_ms    = 0.0;   /* CPU match is host-side; not run here */
    b->last_timing.kernel_ms   = (double)kern;
    b->last_timing.download_ms = (double)dl;
    b->last_timing.total_ms    = (double)tot;
    /* Unconditional, not gated on allow_preupload: an ordinary igpu_recon_run()
     * call (allow_preupload=0) must scope last_preupload_status to THIS run,
     * not leave a prior igpu_recon_run_preuploaded() call's status sticking
     * around. preupload_status is zero-initialized above and only populated
     * by take_exact_preupload() when allow_preupload is set, so this correctly
     * resets to "unavailable" on the plain path. */
    EnterCriticalSection(&b->preupload_lock);
    b->last_preupload_status = preupload_status;
    if (allow_preupload && preupload_slot_index >= 0) {
        b->preupload[preupload_slot_index].state = 0;
    }
    LeaveCriticalSection(&b->preupload_lock);
    return 0;
}

IGPU_API
int igpu_recon_run(igpu_recon_backend* b,
                   const igpu_recon_frame_t* frame,
                   const uint16_t* in_bayer14,
                   igpu_recon_out_kind out_kind,
                   uint16_t* out_bayer16,
                   unsigned int gl_texture)
{
    return igpu_recon_run_internal(b,
                                   0,
                                   0,
                                   frame,
                                   in_bayer14,
                                   out_kind,
                                   out_bayer16,
                                   gl_texture);
}

IGPU_API
int igpu_recon_run_preuploaded(igpu_recon_backend* b,
                               uint64_t frame_id,
                               const igpu_recon_frame_t* frame,
                               const uint16_t* in_bayer14,
                               igpu_recon_out_kind out_kind,
                               uint16_t* out_bayer16,
                               unsigned int gl_texture)
{
    return igpu_recon_run_internal(b,
                                   frame_id,
                                   1,
                                   frame,
                                   in_bayer14,
                                   out_kind,
                                   out_bayer16,
                                   gl_texture);
}

IGPU_API
int igpu_recon_last_preupload_status(igpu_recon_backend* b,
                                     igpu_recon_preupload_status_t* status)
{
    if (!b || !status) return -1;
    EnterCriticalSection(&b->preupload_lock);
    *status = b->last_preupload_status;
    LeaveCriticalSection(&b->preupload_lock);
    return 0;
}

IGPU_API
int igpu_recon_last_timing(igpu_recon_backend* b, igpu_recon_timing_t* t)
{
    if (!b || !t) return -1;
    *t = b->last_timing;
    return 0;
}

IGPU_API
int igpu_recon_last_device_output(igpu_recon_backend* b,
                                  const uint16_t** device_bayer16,
                                  int* width,
                                  int* height)
{
    if (!b || !device_bayer16 || !width || !height || !b->d_out || !b->have_clip) {
        return -1;
    }
    *device_bayer16 = b->d_out;
    *width = b->width;
    *height = b->height;
    return 0;
}

IGPU_API
int igpu_recon_retain_last_device_output(igpu_recon_backend* b,
                                         const uint16_t** device_bayer16,
                                         int* width,
                                         int* height,
                                         uint64_t* token)
{
    if (!b || !device_bayer16 || !width || !height || !token ||
        !b->d_out || !b->have_clip) {
        return -1;
    }
    CK(cudaSetDevice(b->device));

    int slot = -1;
    for (int i = 0; i < RETAINED_DEVICE_OUTPUT_SLOTS; ++i) {
        if (!b->retained_device_output_in_use[i]) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        return 4;
    }

    const size_t n = (size_t)b->width * (size_t)b->height;
    const size_t bytes = n * sizeof(uint16_t);
    if (!b->retained_device_output[slot] ||
        b->retained_device_output_width[slot] != b->width ||
        b->retained_device_output_height[slot] != b->height) {
        if (b->retained_device_output[slot]) {
            cudaFree(b->retained_device_output[slot]);
            b->retained_device_output_allocated_bytes -=
                (uint64_t)b->retained_device_output_width[slot] *
                (uint64_t)b->retained_device_output_height[slot] *
                (uint64_t)sizeof(uint16_t);
            b->retained_device_output[slot] = NULL;
        }
        CK(cudaMalloc(&b->retained_device_output[slot], bytes));
        b->retained_device_output_width[slot] = b->width;
        b->retained_device_output_height[slot] = b->height;
        b->retained_device_output_allocated_bytes += (uint64_t)bytes;
    }

    CK(cudaMemcpy(b->retained_device_output[slot],
                  b->d_out,
                  bytes,
                  cudaMemcpyDeviceToDevice));
    uint64_t next = b->next_retained_device_output_token++;
    if (next == 0) {
        next = b->next_retained_device_output_token++;
    }
    b->retained_device_output_token[slot] = next;
    b->retained_device_output_in_use[slot] = 1;
    *device_bayer16 = b->retained_device_output[slot];
    *width = b->width;
    *height = b->height;
    *token = next;
    return 0;
}

IGPU_API
int igpu_recon_release_retained_device_output(igpu_recon_backend* b,
                                              uint64_t token)
{
    if (!b || token == 0) return -1;
    for (int i = 0; i < RETAINED_DEVICE_OUTPUT_SLOTS; ++i) {
        if (b->retained_device_output_in_use[i] &&
            b->retained_device_output_token[i] == token) {
            b->retained_device_output_in_use[i] = 0;
            b->retained_device_output_token[i] = 0;
            return 0;
        }
    }
    return 1;
}

IGPU_API
int igpu_recon_copy_last_device_output_to_gl_texture(igpu_recon_backend* b,
                                                     unsigned int gl_texture)
{
    if (!b || !b->d_out || !b->have_clip || gl_texture == 0) {
        return -1;
    }
    CK(cudaSetDevice(b->device));
    return copy_bayer16_to_gl_r16_texture(b, gl_texture);
}

IGPU_API
int igpu_recon_allocated_bytes(igpu_recon_backend* b, uint64_t* bytes)
{
    if (!b || !bytes) return -1;
    const uint64_t tracked =
        b->clip_allocated_bytes +
        b->lut_allocated_bytes +
        b->retained_device_output_allocated_bytes +
        b->preupload_device_allocated_bytes;
    *bytes = tracked ? tracked + CUDA_CONTEXT_RESERVE_BYTES : 0;
    return 0;
}

} /* extern "C" */
