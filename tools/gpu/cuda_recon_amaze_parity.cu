/* cuda_recon_amaze_parity.cu - CUDA port + parity probe for the MLV-App Dual
 * ISO full-20-bit reconstruction AMaZE interpolation path (interp_method=0,
 * the PRODUCTION DEFAULT; diso_averaging=0), validated stage-by-stage against
 * the CPU scalar oracle (tools/gpu/oracle, --case amaze*).
 *
 * What this ports
 * ---------------
 * The dualiso-owned, edge-directed AMaZE reconstruction stages
 * (src/mlv/llrawproc/dualiso.c amaze_interpolate, lines ~3011-3279):
 *
 *   1. grayscale   gray[x+y*w] = (uint32)(green[]/2 + red[]/4 + blue[]/4)   (float, trunc)
 *   2. edge_dir    edge_direction[x+y*w] : per-pixel edge-direction search over
 *                  edge_directions[] with the diagonal penalty
 *                  e += ABS(d-d0)*EV_RESOLUTION/8, in EV space (raw2ev[gray]).
 *   3. edge_interp dark/bright[x+y*w] = ev2raw[(2*pi0+pip+pim)/4], where each
 *                  pi = (raw2ev[pa]*2 + raw2ev[pb])/3 over the edge stencil,
 *                  pa/pb = COERCE((int)plane_float[squeezed[y+dy]*wx + x+dx],0,0xFFFFF).
 *   4. border_interpolate, fullres_reconstruction, mix_images(halfres),
 *      build_alias_map, overexposed map, final_blend, convert_20_to_16bit
 *      -- REUSED verbatim from the validated mean23 port (identical downstream).
 *
 * Input handoff (the demosaiced red/green/blue float planes) is taken from the
 * oracle dumps (amaze_red/green/blue.f32). The generic RawTherapee AMaZE
 * demosaic (amaze_demosaic.c, SSE2 float core) is the ONE upstream stage that is
 * not re-ported here -- its float core is the source of the engine's own
 * AVX2/scalar +/-1 LSB internal tolerance and is validated separately via the
 * debayer path. Feeding CUDA the oracle's exact demosaiced planes isolates the
 * dualiso-owned edge-directed reconstruction so its parity can be proven cleanly.
 *
 * Parity strategy (same as the mean23 port)
 *   - raw2ev / ev2raw / mix_curve / fullres_curve(float-cast) /
 *     fullres_curve_double PRECOMPUTED by the oracle and uploaded as-is.
 *   - The only float op in the dualiso-owned AMaZE surface is the grayscale
 *     g/2+r/4+b/4 (single-precision, then truncated to u32). Compile with
 *     --fmad=false for IEEE float parity; the sum order is verbatim.
 *   - Everything else (edge search, edge_interp, downstream) is integer EV-LUT.
 *   - Every stage writes a device plane; we copy it back and diff vs the oracle
 *     dump to localize any mismatch (esp. amaze_gray, amaze_edge_direction, and
 *     stage_dark/stage_bright after edge_interp).
 *
 * Build (on Ultra-Magnus): build-cuda.ps1 -Src cuda_recon_amaze_parity.cu -Extra "--fmad=false"
 * Run: cuda_recon_amaze_parity.exe [vectors_dir]
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <string>
#include <climits>
#include <cuda_runtime.h>

/* ---- fixed LUT geometry (case-independent) ---------------------------- */
#define EV_RESOLUTION 65536
#define RAW2EV_COUNT  (1u << 20)
#define EV2RAW_COUNT  (24u * EV_RESOLUTION)
#define EV2RAW_ORIGIN (10 * EV_RESOLUTION)
#define ALIAS_MAP_MAX 15000
#define HOST_FULLRES_THR 0.8

/* ---- per-case geometry + scalars (loaded from scalars.txt) ------------ */
__device__ __constant__ int   d_W;
__device__ __constant__ int   d_H;
__device__ __constant__ int   d_WX;             /* AMaZE plane stride = w+16 */
__device__ __constant__ int   d_black;
__device__ __constant__ int   d_white;
__device__ __constant__ int   d_white_darkened;
__device__ __constant__ int   d_dark_noise;
__device__ __constant__ int   d_is_bright[4];

static int    HOST_W = 0, HOST_H = 0, HOST_WX = 0;
static int    HOST_BLACK = 0, HOST_WHITE = 0, HOST_WHITE_DARKENED = 0, HOST_DARK_NOISE = 0;
static int    HOST_IS_BRIGHT[4] = {0,0,0,0};
static char   HOST_CASE_NAME[64] = "?";
static int    HOST_VARIANT = 0;
static int    HOST_INTERP = -1;

#define CK(call) do { cudaError_t _e=(call); if(_e!=cudaSuccess){ \
    fprintf(stderr,"CUDA error %s @ %s:%d: %s\n",#call,__FILE__,__LINE__, \
    cudaGetErrorString(_e)); exit(10);} } while(0)

__device__ __host__ static inline int   imin(int a,int b){ return a<b?a:b; }
__device__ __host__ static inline int   imax(int a,int b){ return a>b?a:b; }
__device__ __host__ static inline int   icoerce(int x,int lo,int hi){ return imax(imin(x,hi),lo); }
__device__ __host__ static inline double dmin(double a,double b){ return a<b?a:b; }
__device__ __host__ static inline double dmax(double a,double b){ return a>b?a:b; }
__device__ __host__ static inline double dcoerce(double x,double lo,double hi){ return dmax(dmin(x,hi),lo); }
__device__ __host__ static inline int   iabs_(int a){ return a>0?a:-a; }

/* ======================================================================= *
 *  AMaZE edge_directions[] table (dualiso.c:2762-2776).
 *  Each row: ack(verify near a), a(interp near line), b(interp other line),
 *  bck(verify near b). y coords are multiplied by s (s = +/-1).             */
struct AmazeXY { int x, y; };
struct AmazeDir { AmazeXY ack, a, b, bck; };
__device__ __constant__ AmazeDir d_edge[12];
static const AmazeDir HOST_EDGE[12] = {
    { {-4,2}, {-2,1}, { 4,-2}, { 6,-3} },
    { {-3,2}, {-1,1}, { 3,-2}, { 4,-3} },
    { {-2,2}, {-1,1}, { 2,-2}, { 3,-3} },
    { {-1,2}, {-1,1}, { 1,-2}, { 2,-3} },
    { {-1,2}, { 0,1}, { 1,-2}, { 1,-3} },
    { { 0,2}, { 0,1}, { 0,-2}, { 0,-3} },
    { { 1,2}, { 0,1}, {-1,-2}, {-1,-3} },
    { { 1,2}, { 1,1}, {-1,-2}, {-2,-3} },
    { { 2,2}, { 1,1}, {-2,-2}, {-3,-3} },
    { { 3,2}, { 1,1}, {-3,-2}, {-4,-3} },
    { { 4,2}, { 2,1}, {-4,-2}, {-6,-3} },
};
#define AMAZE_NDIR 11        /* COUNT(edge_directions) = 11 */
#define AMAZE_D0   (AMAZE_NDIR/2)   /* = 5, the vertical (preferred) direction */

/* ======================================================================= *
 *  AMaZE STAGE 0: squeeze remap (squeezed[y]).
 *  Mirrors dualiso.c:2854-2880. Computed on HOST (tiny, serial, h ints) and
 *  uploaded -- it is a serial prefix over rows, trivial and deterministic.   *
 *  (Also cross-checked against the oracle's amaze_squeezed.i32 dump.)         */
static void host_build_squeezed(int* sq, int h, const int* is_bright)
{
    for (int y=0;y<h;y++) sq[y] = -1;
    int yh = -1;
    for (int y=0;y<h;y++) {            /* dark rows (not BRIGHT_ROW) */
        if (is_bright[y&3]) continue;
        if (yh < 0) yh = y;
        sq[y] = yh; yh++;
    }
    yh = -1;
    for (int y=0;y<h;y++) {            /* bright rows */
        if (!is_bright[y&3]) continue;
        if (yh < 0) yh = h/4*2 + y;
        sq[y] = yh; yh++;
        if (yh >= h) break;
    }
    /* second pass: squeezed[y] = 0 where still < 0 (dualiso.c:2908-2912) */
    for (int y=0;y<h;y++) if (sq[y] < 0) sq[y] = 0;
}

/* ======================================================================= *
 *  AMaZE STAGE 1: grayscale gray[x+y*w] = (uint32)(g/2 + r/4 + b/4)
 *  Float single-precision, verbatim op order, truncated to u32. The planes
 *  are flat h*wx storage indexed [squeezed[y]*wx + x].                       */
__global__ void k_amaze_gray(uint32_t* __restrict gray,
                             const float* __restrict red,
                             const float* __restrict green,
                             const float* __restrict blue,
                             const int* __restrict squeezed)
{
    const int W = d_W, H = d_H, WX = d_WX;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t prow = (size_t)squeezed[y]*WX + x;
    /* match C: gray[..] = green/2 + red/4 + blue/4 ; operands float, '/2','/4'
     * are float divisions, sum in float, assigned to uint32 -> trunc toward 0. */
    float g = green[prow] * 0.5f;
    float r = red[prow]   * 0.25f;
    float b = blue[prow]  * 0.25f;
    /* NB the C source writes green[]/2 + red[]/4 + blue[]/4 (left-to-right). The
     * float '/2' and '/4' are exact (powers of two), and the two additions are
     * left-assoc: (g/2 + r/4) + b/4. Reproduce that association exactly. */
    float sum = (g + r) + b;
    gray[(size_t)x + (size_t)y*W] = (uint32_t)(int)sum;   /* trunc toward zero */
}

/* ======================================================================= *
 *  AMaZE STAGE 2: edge-direction selection (dualiso.c:3127-3224 scalar).
 *  Interior y in [5,h-5), x in [5,w-5); elsewhere stays d0 (pre-init).
 *  Per-direction error = sum over j in [-5,5] of |p1-p2|+|p2-p3|+|p3-p4| in EV
 *  space, plus the diagonal penalty ABS(d-d0)*EV_RESOLUTION/8. Strict-`<`
 *  selection keeps the first (lowest-index) direction on ties, exactly as C.   */
__global__ void k_amaze_edgedir_init(uint8_t* __restrict edge_dir)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    edge_dir[(size_t)x + (size_t)y*W] = (uint8_t)AMAZE_D0;
}

__global__ void k_amaze_edgedir(uint8_t* __restrict edge_dir,
                                const uint32_t* __restrict gray,
                                const uint32_t* __restrict raw32, /* match-applied promote */
                                const int* __restrict raw2ev,
                                const double* __restrict fullres_curve_d)
{
    const int W = d_W, H = d_H;
    int x = 5 + blockIdx.x*blockDim.x + threadIdx.x;
    int y = 5 + blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W-5 || y>=H-5) return;

    int s = (d_is_bright[y&3] == d_is_bright[(y+1)&3]) ? -1 : 1;
    int bright_row = d_is_bright[y&3];

    int e_best = INT_MAX;
    int d_best = AMAZE_D0;
    int dmin = 0, dmax = AMAZE_NDIR - 1;
    const int search_area = 5;

    /* same dmin/dmax narrowing as scalar (dualiso.c:3139-3170) */
    if (!bright_row) {
        /* interpolating bright exposure */
        if (fullres_curve_d[raw32[(size_t)x + (size_t)y*W]] > HOST_FULLRES_THR) {
            dmin = AMAZE_D0; dmax = AMAZE_D0;          /* no high accuracy needed */
        }
        /* else deep shadows -> full search */
    } else if (raw32[(size_t)x + (size_t)y*W] < (unsigned)d_white_darkened) {
        dmin = AMAZE_D0; dmax = AMAZE_D0;              /* good bright data */
    }
    /* else semi-overexposed -> full search */

    if (dmin == dmax) {
        d_best = dmin;
    } else {
        for (int d=dmin; d<=dmax; d++) {
            int e = 0;
            for (int j=-search_area; j<=search_area; j++) {
                int dx1 = d_edge[d].ack.x + j; int dy1 = d_edge[d].ack.y * s;
                int p1 = raw2ev[gray[(size_t)(x+dx1) + (size_t)(y+dy1)*W]];
                int dx2 = d_edge[d].a.x + j;   int dy2 = d_edge[d].a.y * s;
                int p2 = raw2ev[gray[(size_t)(x+dx2) + (size_t)(y+dy2)*W]];
                int dx3 = d_edge[d].b.x + j;   int dy3 = d_edge[d].b.y * s;
                int p3 = raw2ev[gray[(size_t)(x+dx3) + (size_t)(y+dy3)*W]];
                int dx4 = d_edge[d].bck.x + j; int dy4 = d_edge[d].bck.y * s;
                int p4 = raw2ev[gray[(size_t)(x+dx4) + (size_t)(y+dy4)*W]];
                e += iabs_(p1-p2) + iabs_(p2-p3) + iabs_(p3-p4);
            }
            e += iabs_(d - AMAZE_D0) * (EV_RESOLUTION/8);
            if (e < e_best) { e_best = e; d_best = d; }
        }
    }
    edge_dir[(size_t)x + (size_t)y*W] = (uint8_t)d_best;
}

/* ======================================================================= *
 *  AMaZE STAGE 3: actual edge_interp -> dark/bright (dualiso.c:3250-3279).
 *  One thread per interior pixel x in [2,w-2), y in [2,h-2). Writes
 *  interp[x+y*w] and native[x+y*w] (native = raw_get_pixel32(x,y)=raw32[x+y*w]).
 *  plane selection: is_rg ? (x even?red:green) : (x even?green:blue).
 *  edge_interp gathers from plane[squeezed[y+dy]*wx + x+dx], COERCE to 20-bit. */
__device__ static inline int amaze_edge_interp(const float* plane, int wx,
                                               const int* squeezed, const int* raw2ev,
                                               int dir, int x, int y, int s, int W)
{
    int dxa = d_edge[dir].a.x;
    int dya = d_edge[dir].a.y * s;
    int pa = icoerce((int)plane[(size_t)squeezed[y+dya]*wx + (x+dxa)], 0, 0xFFFFF);
    int dxb = d_edge[dir].b.x;
    int dyb = d_edge[dir].b.y * s;
    int pb = icoerce((int)plane[(size_t)squeezed[y+dyb]*wx + (x+dxb)], 0, 0xFFFFF);
    int pi = (raw2ev[pa]*2 + raw2ev[pb]) / 3;
    return pi;
}

__global__ void k_amaze_interp(uint32_t* __restrict dark, uint32_t* __restrict bright,
                               const uint32_t* __restrict raw32,
                               const float* __restrict red,
                               const float* __restrict green,
                               const float* __restrict blue,
                               const int* __restrict squeezed,
                               const uint8_t* __restrict edge_dir,
                               const int* __restrict raw2ev,
                               const int* __restrict ev2raw)
{
    const int W = d_W, H = d_H, WX = d_WX;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x<2 || x>=W-2 || y<2 || y>=H-2) return;

    int bright_row = d_is_bright[y&3];
    uint32_t* native = bright_row ? bright : dark;
    uint32_t* interp = bright_row ? dark   : bright;
    int is_rg = (y % 2 == 0);
    int s = (d_is_bright[y&3] == d_is_bright[(y+1)&3]) ? -1 : 1;

    const float* plane = is_rg ? ((x%2==0) ? red   : green)
                               : ((x%2==0) ? green : blue);
    int dir = edge_dir[(size_t)x + (size_t)y*W];
    int dirp = imin(dir+1, AMAZE_NDIR-1);
    int dirm = imax(dir-1, 0);

    int pi0 = amaze_edge_interp(plane, WX, squeezed, raw2ev, dir,  x, y, s, W);
    int pip = amaze_edge_interp(plane, WX, squeezed, raw2ev, dirp, x, y, s, W);
    int pim = amaze_edge_interp(plane, WX, squeezed, raw2ev, dirm, x, y, s, W);

    interp[(size_t)x + (size_t)y*W] = ev2raw[(2*pi0+pip+pim)/4];
    native[(size_t)x + (size_t)y*W] = raw32[(size_t)x + (size_t)y*W];
}

/* ======================================================================= *
 *  DOWNSTREAM STAGES (verbatim from the validated mean23 port)             *
 *  convert_to_20bit+match, border_interpolate, fullres, mix halfres,       *
 *  build_alias_map (init/rank/gauss/gray), overexposed, final_blend,       *
 *  convert_20_to_16bit.                                                    */

/* match apply needs the factor; for AMaZE we still apply it (same driver path).
 * black_delta20=0 for this config. factor loaded from scalars. */
__device__ __constant__ double d_factor;
__device__ __constant__ int    d_black_delta20;

__global__ void k_promote_match(const uint16_t* __restrict in, uint32_t* __restrict raw32)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i = (size_t)x + (size_t)y*W;
    int p = (int)((((uint32_t)in[i]) << 6) & 0xFFFFFu);
    if (p != 0) {
        if (d_is_bright[y & 3]) {
            p = (int)(((double)(p - d_black + d_black_delta20)) * d_factor + (double)d_black);
        } else {
            p = (int)((double)(p - d_black_delta20) + (double)d_black_delta20 * d_factor);
        }
    }
    raw32[i] = (uint32_t)icoerce(p, 0, 0xFFFFF);
}

__global__ void k_border_toprows(const uint32_t* __restrict raw32,
                                 uint32_t* __restrict dark, uint32_t* __restrict bright)
{
    const int W = d_W;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=3) return;
    int br = d_is_bright[y & 3];
    uint32_t* native = br ? bright : dark;
    uint32_t* interp = br ? dark : bright;
    interp[x + y*W] = raw32[x + (y+2)*W];
    native[x + y*W] = raw32[x + y*W];
}
__global__ void k_border_botrows(const uint32_t* __restrict raw32,
                                 uint32_t* __restrict dark, uint32_t* __restrict bright)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int yy = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || yy>=4) return;
    int y = H-4+yy;
    int br = d_is_bright[y & 3];
    uint32_t* native = br ? bright : dark;
    uint32_t* interp = br ? dark : bright;
    interp[x + y*W] = raw32[x + (y-2)*W];
    native[x + y*W] = raw32[x + y*W];
}
__global__ void k_border_cols(const uint32_t* __restrict raw32,
                              uint32_t* __restrict dark, uint32_t* __restrict bright)
{
    const int W = d_W, H = d_H;
    int y = blockIdx.x*blockDim.x + threadIdx.x;
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

__global__ void k_fullres(uint32_t* __restrict fullres,
                          const uint32_t* __restrict dark, const uint32_t* __restrict bright)
{
    const int W = d_W, H = d_H;
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

__global__ void k_mix_halfres(uint32_t* __restrict halfres,
                              const uint32_t* __restrict bright, const uint32_t* __restrict dark,
                              const int* __restrict raw2ev, const int* __restrict ev2raw,
                              const double* __restrict mix_curve)
{
    const int W = d_W, H = d_H;
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

__global__ void k_alias_init(uint16_t* __restrict alias_map,
                             const uint32_t* __restrict fullres_smooth,
                             const uint32_t* __restrict halfres_smooth,
                             const uint32_t* __restrict bright,
                             const int* __restrict raw2ev,
                             const double* __restrict fullres_curve_d)
{
    const int W = d_W, H = d_H;
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
                             const double* __restrict fullres_curve_d)
{
    const int W = d_W, H = d_H;
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
                              const double* __restrict fullres_curve_d)
{
    const int W = d_W, H = d_H;
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
    alias_map[x + (size_t)y*W] = (uint16_t)imin(c, 0xFFFF);  /* saturate like the CPU, never wrap */
}

__global__ void k_alias_gray(uint16_t* __restrict alias_map)
{
    const int W = d_W, H = d_H;
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

__global__ void k_over_mark(uint16_t* __restrict over_aux,
                            const uint32_t* __restrict bright, const uint32_t* __restrict dark)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    over_aux[i] = (bright[i] >= (uint32_t)d_white_darkened || dark[i] >= (uint32_t)d_white) ? 100 : 0;
}
__global__ void k_over_bordercopy(uint16_t* __restrict overexposed,
                                  const uint16_t* __restrict over_aux)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    if (y<3 || y>=H-3) { overexposed[i] = over_aux[i]; return; }
    if (x<3 || x>=W-3) { overexposed[i] = over_aux[i]; return; }
}
__global__ void k_over_blur(uint16_t* __restrict overexposed,
                            const uint16_t* __restrict over_aux)
{
    const int W = d_W, H = d_H;
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

__global__ void k_final_blend(uint32_t* __restrict raw32,
                              const uint32_t* __restrict bright,
                              const uint32_t* __restrict halfres_smooth,
                              const uint32_t* __restrict fullres,
                              const uint32_t* __restrict fullres_smooth,
                              const uint32_t* __restrict dark,
                              const uint16_t* __restrict overexposed,
                              const uint16_t* __restrict alias_map,
                              const int* __restrict raw2ev, const int* __restrict ev2raw,
                              const double* __restrict fullres_curve_f)
{
    const int W = d_W, H = d_H;
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
    { int co = (int)alias_map[i]; c = dcoerce((double)co / (double)ALIAS_MAP_MAX, 0.0, 1.0); }
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

__global__ void k_convert16(uint16_t* __restrict out, const uint32_t* __restrict raw32)
{
    const int W = d_W, H = d_H;
    int x = blockIdx.x*blockDim.x + threadIdx.x;
    int y = blockIdx.y*blockDim.y + threadIdx.y;
    if (x>=W || y>=H) return;
    size_t i=(size_t)x+(size_t)y*W;
    double v = (double)raw32[i];
    int o = (int)(v/16.0 + 0.5);
    out[i] = (uint16_t)icoerce(o, 0, 0xFFFF);
}

/* ----------------------------------------------------------------------- *
 *  host helpers                                                            */
static void* load_blob(const std::string& path, size_t expect_bytes)
{
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) { fprintf(stderr,"cannot open %s\n", path.c_str()); exit(3); }
    fseek(f,0,SEEK_END); long sz=ftell(f); fseek(f,0,SEEK_SET);
    if (expect_bytes && (size_t)sz != expect_bytes) {
        fprintf(stderr,"size mismatch %s: got %ld want %zu\n", path.c_str(), sz, expect_bytes);
        exit(3);
    }
    void* p = malloc(sz);
    if (fread(p,1,sz,f)!=(size_t)sz){ fprintf(stderr,"short read %s\n",path.c_str()); exit(3); }
    fclose(f);
    return p;
}

static int parse_kv_int(const char* line, const char* key, int* out)
{
    size_t kl = strlen(key);
    if (strncmp(line, key, kl) != 0 || line[kl] != '=') return 0;
    *out = atoi(line + kl + 1);
    return 1;
}
static int parse_kv_double(const char* line, const char* key, double* out)
{
    size_t kl = strlen(key);
    if (strncmp(line, key, kl) != 0 || line[kl] != '=') return 0;
    *out = atof(line + kl + 1);
    return 1;
}
static double HOST_FACTOR = 0.0;
static int    HOST_BLACK_DELTA20 = 0;
static void load_scalars(const std::string& path)
{
    FILE* f = fopen(path.c_str(), "r");
    if (!f) { fprintf(stderr, "cannot open scalars %s\n", path.c_str()); exit(3); }
    char line[512];
    int got_w=0,got_h=0,got_wx=0,got_black=0,got_white=0,got_wd=0,got_dn=0,
        got_factor=0,got_bd=0,got_isb=0,got_interp=0;
    while (fgets(line, sizeof(line), f)) {
        char* p = line; while (*p==' '||*p=='\t') p++;
        if (*p=='#' || *p=='\n' || *p=='\0') continue;
        int iv; double dv;
        if (parse_kv_int(p,"width",&iv))            { HOST_W=iv; got_w=1; continue; }
        if (parse_kv_int(p,"height",&iv))           { HOST_H=iv; got_h=1; continue; }
        if (parse_kv_int(p,"amaze_row_width",&iv))  { HOST_WX=iv; got_wx=1; continue; }
        if (parse_kv_int(p,"black20",&iv))          { HOST_BLACK=iv; got_black=1; continue; }
        if (parse_kv_int(p,"white20",&iv))          { HOST_WHITE=iv; got_white=1; continue; }
        if (parse_kv_int(p,"white_darkened",&iv))   { HOST_WHITE_DARKENED=iv; got_wd=1; continue; }
        if (parse_kv_int(p,"dark_noise20",&iv))     { HOST_DARK_NOISE=iv; got_dn=1; continue; }
        if (parse_kv_double(p,"factor",&dv))        { HOST_FACTOR=dv; got_factor=1; continue; }
        if (parse_kv_int(p,"black_delta20",&iv))    { HOST_BLACK_DELTA20=iv; got_bd=1; continue; }
        if (parse_kv_int(p,"pattern_variant",&iv))  { HOST_VARIANT=iv; continue; }
        if (parse_kv_int(p,"interp_method",&iv))    { HOST_INTERP=iv; got_interp=1; continue; }
        if (strncmp(p,"case=",5)==0) { sscanf(p+5,"%63[^\n]",HOST_CASE_NAME); continue; }
        if (strncmp(p,"is_bright=",10)==0) {
            int a,b,c,d;
            if (sscanf(p+10,"%d,%d,%d,%d",&a,&b,&c,&d)==4) {
                HOST_IS_BRIGHT[0]=a;HOST_IS_BRIGHT[1]=b;HOST_IS_BRIGHT[2]=c;HOST_IS_BRIGHT[3]=d; got_isb=1;
            }
            continue;
        }
    }
    fclose(f);
    if (!(got_w&&got_h&&got_wx&&got_black&&got_white&&got_wd&&got_dn&&got_factor&&got_bd&&got_isb&&got_interp)) {
        fprintf(stderr,"scalars.txt missing required key(s): w=%d h=%d wx=%d black20=%d white20=%d wd=%d dn=%d factor=%d bd=%d isb=%d interp=%d\n",
                got_w,got_h,got_wx,got_black,got_white,got_wd,got_dn,got_factor,got_bd,got_isb,got_interp);
        exit(3);
    }
    if (HOST_INTERP != 0) {
        fprintf(stderr,"this probe is for AMaZE (interp_method=0) cases; scalars say interp_method=%d\n", HOST_INTERP);
        exit(3);
    }
}

template<typename T>
static long long diff_stage(const char* name, const T* dev_host, const T* oracle, size_t n)
{
    const int W = HOST_W;
    long long maxabs=0; double sumabs=0; size_t nz=0,fx=0,fy=0; long long firstd=0; int found=0;
    for (size_t i=0;i<n;i++){
        long long d=(long long)dev_host[i]-(long long)oracle[i];
        long long a=d<0?-d:d;
        if (a>maxabs) maxabs=a;
        sumabs+=(double)a;
        if (a){ nz++; if(!found){found=1;fx=i%W;fy=i/W;firstd=d;} }
    }
    printf("  [stage %-20s] max|d|=%-8lld mean|d|=%.6f  mismatched=%zu/%zu",
           name, maxabs, sumabs/(double)n, nz, n);
    if (found) printf("  first@(%zu,%zu) d=%lld", fx, fy, firstd);
    printf("\n");
    return maxabs;
}

int main(int argc, char** argv)
{
    std::string vdir = "G:\\Temp\\mlv-gpu-profile\\oracle\\vectors\\amaze";
    if (argc>=2) vdir = argv[1];
    auto P=[&](const char* n){ return vdir + "\\" + n; };

    load_scalars(P("scalars.txt"));
    const int W=HOST_W, H=HOST_H, WX=HOST_WX;
    const size_t n=(size_t)W*H;
    const size_t plane=(size_t)H*WX;
    printf("[amaze-parity] vectors dir: %s\n", vdir.c_str());
    printf("[amaze-parity] case=%s variant=%d interp=%d  geometry %dx%d (wx=%d, %zu px)\n",
           HOST_CASE_NAME, HOST_VARIANT, HOST_INTERP, W, H, WX, n);
    printf("[amaze-parity] scalars: black20=%d white20=%d white_darkened=%d dark_noise20=%d "
           "factor=%.17g black_delta20=%d is_bright={%d,%d,%d,%d}\n",
           HOST_BLACK,HOST_WHITE,HOST_WHITE_DARKENED,HOST_DARK_NOISE,HOST_FACTOR,
           HOST_BLACK_DELTA20,HOST_IS_BRIGHT[0],HOST_IS_BRIGHT[1],HOST_IS_BRIGHT[2],HOST_IS_BRIGHT[3]);

    cudaDeviceProp prop; CK(cudaGetDeviceProperties(&prop,0));
    printf("[amaze-parity] device: %s sm_%d%d (--fmad should be false)\n", prop.name, prop.major, prop.minor);

    /* upload geometry + scalars + tables */
    CK(cudaMemcpyToSymbol(d_W,&HOST_W,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_H,&HOST_H,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_WX,&HOST_WX,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_black,&HOST_BLACK,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white,&HOST_WHITE,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_white_darkened,&HOST_WHITE_DARKENED,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_dark_noise,&HOST_DARK_NOISE,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_factor,&HOST_FACTOR,sizeof(double)));
    CK(cudaMemcpyToSymbol(d_black_delta20,&HOST_BLACK_DELTA20,sizeof(int)));
    CK(cudaMemcpyToSymbol(d_is_bright,HOST_IS_BRIGHT,sizeof(int)*4));
    CK(cudaMemcpyToSymbol(d_edge,HOST_EDGE,sizeof(HOST_EDGE)));

    /* load inputs + LUTs + AMaZE planes */
    uint16_t* h_in  = (uint16_t*)load_blob(P("in.u16"),  n*sizeof(uint16_t));
    uint16_t* h_out = (uint16_t*)load_blob(P("out.u16"), n*sizeof(uint16_t));
    int*    h_raw2ev = (int*)   load_blob(P("raw2ev.i32"), (size_t)RAW2EV_COUNT*sizeof(int));
    int*    h_ev2raw = (int*)   load_blob(P("ev2raw.i32"), (size_t)EV2RAW_COUNT*sizeof(int));
    double* h_mix    = (double*)load_blob(P("mix_curve.f64"), (size_t)RAW2EV_COUNT*sizeof(double));
    double* h_frc_f  = (double*)load_blob(P("fullres_curve.f64"), (size_t)RAW2EV_COUNT*sizeof(double));
    double* h_frc_d  = (double*)load_blob(P("fullres_curve_double.f64"), (size_t)RAW2EV_COUNT*sizeof(double));

    /* AMaZE demosaiced planes + squeezed (handoff from the oracle's real demosaic) */
    float* h_red    = (float*)load_blob(P("amaze_red.f32"),    plane*sizeof(float));
    float* h_green  = (float*)load_blob(P("amaze_green.f32"),  plane*sizeof(float));
    float* h_blue   = (float*)load_blob(P("amaze_blue.f32"),   plane*sizeof(float));
    int*   h_squeez = (int*)  load_blob(P("amaze_squeezed.i32"), (size_t)H*sizeof(int));

    /* cross-check host squeeze == oracle squeeze (sanity) */
    {
        int* hs=(int*)malloc((size_t)H*sizeof(int));
        host_build_squeezed(hs,H,HOST_IS_BRIGHT);
        size_t mism=0; for(int y=0;y<H;y++) if(hs[y]!=h_squeez[y]) mism++;
        printf("[amaze-parity] squeezed remap host-vs-oracle mismatches: %zu/%d %s\n",
               mism, H, mism?"(!! check)":"(ok)");
        free(hs);
    }

    /* oracle stage dumps */
    auto try_load=[&](const char* n_, size_t bytes)->void*{
        FILE* f=fopen(P(n_).c_str(),"rb"); if(!f){ printf("  (dump %s missing - skipping)\n",n_); return NULL;} fclose(f);
        return load_blob(P(n_),bytes);
    };
    uint32_t* o_gray   =(uint32_t*)try_load("amaze_gray.u32", n*sizeof(uint32_t));
    uint8_t*  o_edge   =(uint8_t*) try_load("amaze_edge_direction.u8", n*sizeof(uint8_t));
    uint32_t* o_dark   =(uint32_t*)try_load("stage_dark.u32", n*sizeof(uint32_t));
    uint32_t* o_bright =(uint32_t*)try_load("stage_bright.u32", n*sizeof(uint32_t));
    uint32_t* o_fullres=(uint32_t*)try_load("stage_fullres.u32",n*sizeof(uint32_t));
    uint32_t* o_halfres=(uint32_t*)try_load("stage_halfres.u32",n*sizeof(uint32_t));
    uint16_t* o_alias  =(uint16_t*)try_load("stage_alias_map.u16",n*sizeof(uint16_t));
    uint16_t* o_over   =(uint16_t*)try_load("stage_overexposed.u16",n*sizeof(uint16_t));
    uint32_t* o_final20=(uint32_t*)try_load("stage_final20.u32",n*sizeof(uint32_t));

    /* device buffers */
    uint16_t *d_in,*d_out,*d_alias,*d_over,*d_overaux;
    uint32_t *d_raw32,*d_dark,*d_bright,*d_fullres,*d_halfres,*d_gray;
    uint8_t  *d_edge;
    float    *d_red,*d_green,*d_blue;
    int      *d_squeez,*dd_raw2ev,*dd_ev2raw;
    double   *dd_mix,*dd_frc_f,*dd_frc_d;
    CK(cudaMalloc(&d_in,n*sizeof(uint16_t)));
    CK(cudaMalloc(&d_out,n*sizeof(uint16_t)));
    CK(cudaMalloc(&d_alias,n*sizeof(uint16_t)));
    CK(cudaMalloc(&d_over,n*sizeof(uint16_t)));
    CK(cudaMalloc(&d_overaux,n*sizeof(uint16_t)));
    CK(cudaMalloc(&d_raw32,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_dark,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_bright,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_fullres,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_halfres,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_gray,n*sizeof(uint32_t)));
    CK(cudaMalloc(&d_edge,n*sizeof(uint8_t)));
    CK(cudaMalloc(&d_red,plane*sizeof(float)));
    CK(cudaMalloc(&d_green,plane*sizeof(float)));
    CK(cudaMalloc(&d_blue,plane*sizeof(float)));
    CK(cudaMalloc(&d_squeez,(size_t)H*sizeof(int)));
    CK(cudaMalloc(&dd_raw2ev,(size_t)RAW2EV_COUNT*sizeof(int)));
    CK(cudaMalloc(&dd_ev2raw,(size_t)EV2RAW_COUNT*sizeof(int)));
    CK(cudaMalloc(&dd_mix,(size_t)RAW2EV_COUNT*sizeof(double)));
    CK(cudaMalloc(&dd_frc_f,(size_t)RAW2EV_COUNT*sizeof(double)));
    CK(cudaMalloc(&dd_frc_d,(size_t)RAW2EV_COUNT*sizeof(double)));

    CK(cudaMemcpy(d_in,h_in,n*sizeof(uint16_t),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_red,h_red,plane*sizeof(float),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_green,h_green,plane*sizeof(float),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_blue,h_blue,plane*sizeof(float),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(d_squeez,h_squeez,(size_t)H*sizeof(int),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dd_raw2ev,h_raw2ev,(size_t)RAW2EV_COUNT*sizeof(int),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dd_ev2raw,h_ev2raw,(size_t)EV2RAW_COUNT*sizeof(int),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dd_mix,h_mix,(size_t)RAW2EV_COUNT*sizeof(double),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dd_frc_f,h_frc_f,(size_t)RAW2EV_COUNT*sizeof(double),cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dd_frc_d,h_frc_d,(size_t)RAW2EV_COUNT*sizeof(double),cudaMemcpyHostToDevice));

    int* dd_ev2raw_origin = dd_ev2raw + EV2RAW_ORIGIN;

    CK(cudaMemset(d_alias,0,n*sizeof(uint16_t)));
    CK(cudaMemset(d_dark,0,n*sizeof(uint32_t)));      /* interp_method=0 zeroes dark/bright */
    CK(cudaMemset(d_bright,0,n*sizeof(uint32_t)));
    CK(cudaMemset(d_fullres,0,n*sizeof(uint32_t)));
    CK(cudaMemset(d_halfres,0,n*sizeof(uint32_t)));
    CK(cudaMemset(d_over,0,n*sizeof(uint16_t)));
    CK(cudaMemset(d_overaux,0,n*sizeof(uint16_t)));
    CK(cudaMemset(d_gray,0,n*sizeof(uint32_t)));

    dim3 bt(16,16);
    dim3 gt((W+15)/16,(H+15)/16);
    uint32_t* hb32=(uint32_t*)malloc(n*sizeof(uint32_t));
    uint16_t* hb16=(uint16_t*)malloc(n*sizeof(uint16_t));
    uint8_t*  hb8 =(uint8_t*) malloc(n*sizeof(uint8_t));

    printf("\n[amaze-parity] running kernel chain + per-stage diffs:\n");

    /* STAGE 1+2: promote + match (raw32 = match-applied promote) */
    k_promote_match<<<gt,bt>>>(d_in,d_raw32); CK(cudaDeviceSynchronize()); CK(cudaGetLastError());

    /* AMaZE STAGE 1: grayscale */
    k_amaze_gray<<<gt,bt>>>(d_gray,d_red,d_green,d_blue,d_squeez);
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_gray){ CK(cudaMemcpy(hb32,d_gray,n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("amaze_gray",hb32,o_gray,n); }

    /* AMaZE STAGE 2: edge-direction */
    k_amaze_edgedir_init<<<gt,bt>>>(d_edge); CK(cudaDeviceSynchronize());
    { dim3 g((W-10+15)/16,(H-10+15)/16); k_amaze_edgedir<<<g,bt>>>(d_edge,d_gray,d_raw32,dd_raw2ev,dd_frc_d); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_edge){ CK(cudaMemcpy(hb8,d_edge,n*sizeof(uint8_t),cudaMemcpyDeviceToHost)); diff_stage("amaze_edge_dir",hb8,o_edge,n); }

    /* AMaZE STAGE 3: actual edge_interp -> dark/bright */
    k_amaze_interp<<<gt,bt>>>(d_dark,d_bright,d_raw32,d_red,d_green,d_blue,d_squeez,d_edge,dd_raw2ev,dd_ev2raw_origin);
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());

    /* STAGE 4: border (top, bottom, then cols) */
    { dim3 g((W+15)/16,(3+15)/16); k_border_toprows<<<g,bt>>>(d_raw32,d_dark,d_bright); }
    { dim3 g((W+15)/16,(4+15)/16); k_border_botrows<<<g,bt>>>(d_raw32,d_dark,d_bright); }
    CK(cudaDeviceSynchronize());
    { int t=256; dim3 g((H+t-1)/t); k_border_cols<<<g,t>>>(d_raw32,d_dark,d_bright); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());

    if (o_bright){ CK(cudaMemcpy(hb32,d_bright,n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("bright(post-interp)",hb32,o_bright,n); }
    if (o_dark)  { CK(cudaMemcpy(hb32,d_dark,  n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("dark(post-interp)",  hb32,o_dark,  n); }

    /* STAGE 5: fullres */
    k_fullres<<<gt,bt>>>(d_fullres,d_dark,d_bright); CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_fullres){ CK(cudaMemcpy(hb32,d_fullres,n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("fullres",hb32,o_fullres,n); }

    /* STAGE 6: mix halfres */
    k_mix_halfres<<<gt,bt>>>(d_halfres,d_bright,d_dark,dd_raw2ev,dd_ev2raw_origin,dd_mix);
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_halfres){ CK(cudaMemcpy(hb32,d_halfres,n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("halfres",hb32,o_halfres,n); }

    /* STAGE 7: alias map */
    k_alias_init<<<gt,bt>>>(d_alias,d_fullres,d_halfres,d_bright,dd_raw2ev,dd_frc_d);
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    uint16_t* d_aliasaux; CK(cudaMalloc(&d_aliasaux,n*sizeof(uint16_t)));
    CK(cudaMemcpy(d_aliasaux,d_alias,n*sizeof(uint16_t),cudaMemcpyDeviceToDevice));
    { dim3 g((W-12+15)/16,(H-12+15)/16); k_alias_rank<<<g,bt>>>(d_alias,d_aliasaux,d_bright,dd_frc_d); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    { dim3 g((W-12+15)/16,(H-12+15)/16); k_alias_gauss<<<g,bt>>>(d_alias,d_aliasaux,d_bright,dd_frc_d); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    { int nx=(W-2-2)/2+1, ny=(H-2-2)/2+1; dim3 g((nx+15)/16,(ny+15)/16); k_alias_gray<<<g,bt>>>(d_alias); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_alias){ CK(cudaMemcpy(hb16,d_alias,n*sizeof(uint16_t),cudaMemcpyDeviceToHost)); diff_stage("alias_map",hb16,o_alias,n); }

    /* STAGE 8: overexposed */
    k_over_mark<<<gt,bt>>>(d_overaux,d_bright,d_dark); CK(cudaDeviceSynchronize());
    k_over_bordercopy<<<gt,bt>>>(d_over,d_overaux); CK(cudaDeviceSynchronize());
    { dim3 g((W-6+15)/16,(H-6+15)/16); k_over_blur<<<g,bt>>>(d_over,d_overaux); }
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_over){ CK(cudaMemcpy(hb16,d_over,n*sizeof(uint16_t),cudaMemcpyDeviceToHost)); diff_stage("overexposed",hb16,o_over,n); }

    /* STAGE 9: final blend */
    k_final_blend<<<gt,bt>>>(d_raw32,d_bright,d_halfres,d_fullres,d_fullres,d_dark,
                             d_over,d_alias,dd_raw2ev,dd_ev2raw_origin,dd_frc_f);
    CK(cudaDeviceSynchronize()); CK(cudaGetLastError());
    if (o_final20){ CK(cudaMemcpy(hb32,d_raw32,n*sizeof(uint32_t),cudaMemcpyDeviceToHost)); diff_stage("final20",hb32,o_final20,n); }

    /* STAGE 10: convert 20->16 */
    k_convert16<<<gt,bt>>>(d_out,d_raw32); CK(cudaDeviceSynchronize()); CK(cudaGetLastError());

    /* final out.u16 parity */
    CK(cudaMemcpy(hb16,d_out,n*sizeof(uint16_t),cudaMemcpyDeviceToHost));
    long long maxabs=0; double sumabs=0; size_t le0=0,le1=0,le2=0,le3=0;
    size_t fx=0,fy=0; long long fd=0; int found=0;
    for (size_t i=0;i<n;i++){
        long long d=(long long)hb16[i]-(long long)h_out[i];
        long long a=d<0?-d:d;
        if (a>maxabs) maxabs=a;
        sumabs+=(double)a;
        if (a<=0) le0++;
        if (a<=1) le1++;
        if (a<=2) le2++;
        if (a<=3) le3++;
        if (a>0 && !found){found=1;fx=i%W;fy=i/W;fd=d;}
    }
    printf("\n[amaze-parity] FINAL out.u16 vs oracle (AMaZE):\n");
    printf("  max abs diff   = %lld LSB\n", maxabs);
    printf("  mean abs diff  = %.6f LSB\n", sumabs/(double)n);
    printf("  within 0 LSB   = %.4f%% (%zu/%zu)\n", 100.0*le0/n, le0, n);
    printf("  within 1 LSB   = %.4f%% (%zu/%zu)\n", 100.0*le1/n, le1, n);
    printf("  within 2 LSB   = %.4f%% (%zu/%zu)\n", 100.0*le2/n, le2, n);
    printf("  within 3 LSB   = %.4f%% (%zu/%zu)\n", 100.0*le3/n, le3, n);
    if (found) printf("  first mismatch @(%zu,%zu) dev-oracle=%lld\n", fx, fy, fd);

    printf("\n[amaze-parity] RESULT: %s (<=2 LSB AVX2-tolerance target)\n", (maxabs<=2)?"PASS":"NEEDS-WORK");
    return (maxabs<=2)?0:1;
}
