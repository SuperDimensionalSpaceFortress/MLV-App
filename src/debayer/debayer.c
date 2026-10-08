#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>

#include "debayer.h"
#include "librtprocesswrapper.h"

#if defined(__GNUC__) && !defined(__clang__) && (defined(__x86_64__) || defined(__i386__))
#include <immintrin.h>
#define DEBAYER_AVX2_AVAILABLE 1
#endif

#define MIN(X, Y) (((X) < (Y)) ? (X) : (Y))
#define MAX(X, Y) (((X) > (Y)) ? (X) : (Y))
#define LIMIT16(X) MAX(MIN(X, 65535), 0)

/* ------------------------------------------------------------------------
 * AVX2 fast path for the bilinear debayer (debayerBasicU16).
 *
 * The scalar inner loop per inner step does ~50 integer ops to produce 4
 * RGB pixels (12 uint16 outputs) using a fixed pattern of overlapping
 * 4-input and 2-input averages on the 4x4 Bayer neighbourhood. GCC -O2
 * autovec with -mssse3 declines to vectorise this loop at all (verified
 * via objdump on the 0xa50..0xd60 disassembly window in build-3slot/obj/
 * debayer.o pre-Phase-2B), so the wins come from explicit SIMD.
 *
 * Strategy: 256-bit AVX2 vectors of 16 uint16 lanes, one SIMD iter
 * processes 8 blocks (16 source columns -> 16 RGB output columns per
 * row x 2 rows = 32 RGB pixels = 96 uint16). Each lane k maps to output
 * column (x_anchor + k); the TL/BL formulas apply at even lanes, the
 * TR/BR formulas at odd lanes. We compute both formulas on all 16 lanes
 * then merge via _mm256_blend_epi16 with mask 0xAA.
 *
 * 4-input averages use 32-bit unpack/add/srli/repack for bit-exact
 * parity with scalar's `(a+b+c+d) >> 2` truncation. 2-input averages
 * use the avg-of-avg trick `(a+b)>>1 = avg_epu16(a,b) - ((a^b)&1)` for
 * matching parity (avg_epu16 rounds half-up, scalar truncates).
 *
 * AoS-3 RGB stores: compute six 16-lane channel vectors per iter
 * (top R/G/B, bot R/G/B), spill to 32-byte-aligned scratch arrays, then
 * write 16 RGB triplets per row scalarly. The scalar interleave runs
 * with hot L1 cache since it reads data just stored, and gets autovec'd
 * by GCC for the simple stride-3 store pattern.
 *
 * Edge handling: the first and last 1-pixel column borders, and the
 * top/bottom row pair, fall back to the scalar code (matched memcpy
 * post-fixup, identical to the original kernel).
 *
 * Dispatch: pthread_once latch, default-on for AVX2+FMA hosts. Kill
 * switches MLVAPP_DISABLE_AVX2 (global) and MLVAPP_DISABLE_AVX2_DEBAYER
 * (per-kernel) force the scalar fallback.
 * ------------------------------------------------------------------------ */

/* Per row-pair body: processes one Y (= flat pixel offset of the
 * top row in the 2x2 block). Caller drives the outer loop over Y. */
typedef void (*debayer_basic_u16_rows_fn_t)(uint16_t * __restrict debayerto,
                                            uint16_t * __restrict bayerdata,
                                            int width,
                                            int Y);

static void debayer_basic_u16_rows_scalar(uint16_t * __restrict debayerto,
                                          uint16_t * __restrict bayerdata,
                                          int width,
                                          int Y)
{
    const int widthDB = width - 1;
    const int nextRowRGB = width * 3;
    {
        for (int x = 1; x < widthDB; x += 2)
        {
            int pix = Y + x;
            int pixm1 = pix - width;
            int pixp1 = pix + width;
            int pixp2 = pixp1 + width;

            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + 2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+2 ),
            };

            int rgbPix[4] = {
                (bPix[5] * 3), (bPix[ 6] * 3),
                (bPix[9] * 3), (bPix[10] * 3)
            };

            debayerto[ rgbPix[0] ] = (uint32_t)(
                  bayerdata[ bPix[0] ] + bayerdata[ bPix[ 2] ]
                + bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 2;
            debayerto[ rgbPix[0]+1 ] = (uint32_t)(
                  bayerdata[ bPix[1] ] + bayerdata[ bPix[6] ]
                + bayerdata[ bPix[4] ] + bayerdata[ bPix[9] ]
            ) >> 2;
            debayerto[ rgbPix[0]+2 ] = bayerdata[ bPix[5] ];

            debayerto[ rgbPix[1] ] = (uint32_t)(
                bayerdata[ bPix[2] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            debayerto[ rgbPix[1]+1 ] = bayerdata[ bPix[6] ];
            debayerto[ rgbPix[1]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[7] ]
            ) >> 1;

            debayerto[ rgbPix[2] ] = (uint32_t)(
                bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            debayerto[ rgbPix[2]+1 ] = bayerdata[ bPix[9] ];
            debayerto[ rgbPix[2]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[13] ]
            ) >> 1;

            debayerto[ rgbPix[3] ] = bayerdata[ bPix[10] ];
            debayerto[ rgbPix[3]+1 ] = (uint32_t)(
                  bayerdata[ bPix[ 6] ] + bayerdata[ bPix[ 9] ]
                + bayerdata[ bPix[11] ] + bayerdata[ bPix[14] ]
            ) >> 2;
            debayerto[ rgbPix[3]+2 ] = (uint32_t)(
                  bayerdata[ bPix[ 5] ] + bayerdata[ bPix[ 7] ]
                + bayerdata[ bPix[13] ] + bayerdata[ bPix[15] ]
            ) >> 2;
        }

        uint16_t * edgePixel = debayerto + (3 * Y);
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        edgePixel += nextRowRGB;
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];
        edgePixel += nextRowRGB;
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];
    }
}

#ifdef DEBAYER_AVX2_AVAILABLE

/* Compute (a+b+c+d) >> 2 with bit-exact parity to scalar's truncating
 * cast. Uses 32-bit unpack to avoid 16-bit overflow and the rounding
 * bias that double-_mm256_avg_epu16 would introduce. */
__attribute__((target("avx2,fma")))
static inline __m256i debayer_avg4_u16_truncate(__m256i a, __m256i b, __m256i c, __m256i d)
{
    const __m256i z = _mm256_setzero_si256();
    __m256i a_lo = _mm256_unpacklo_epi16(a, z);
    __m256i a_hi = _mm256_unpackhi_epi16(a, z);
    __m256i b_lo = _mm256_unpacklo_epi16(b, z);
    __m256i b_hi = _mm256_unpackhi_epi16(b, z);
    __m256i c_lo = _mm256_unpacklo_epi16(c, z);
    __m256i c_hi = _mm256_unpackhi_epi16(c, z);
    __m256i d_lo = _mm256_unpacklo_epi16(d, z);
    __m256i d_hi = _mm256_unpackhi_epi16(d, z);
    __m256i s_lo = _mm256_add_epi32(_mm256_add_epi32(a_lo, b_lo),
                                    _mm256_add_epi32(c_lo, d_lo));
    __m256i s_hi = _mm256_add_epi32(_mm256_add_epi32(a_hi, b_hi),
                                    _mm256_add_epi32(c_hi, d_hi));
    s_lo = _mm256_srli_epi32(s_lo, 2);
    s_hi = _mm256_srli_epi32(s_hi, 2);
    /* Pack 32-bit -> 16-bit. With s_lo coming from unpacklo (src lanes
     * [0..3, 8..11]) and s_hi from unpackhi (src lanes [4..7, 12..15]),
     * _mm256_packus_epi32 lays out:
     *   result[0..3]   = s_lo low 128 (= src[0..3])
     *   result[4..7]   = s_hi low 128 (= src[4..7])
     *   result[8..11]  = s_lo high 128 (= src[8..11])
     *   result[12..15] = s_hi high 128 (= src[12..15])
     * which is exactly src[0..15] in order, so no permute needed. */
    return _mm256_packus_epi32(s_lo, s_hi);
}

/* Compute (a+b) >> 1 with bit-exact parity to scalar's truncating cast.
 * _mm256_avg_epu16 rounds half-up (= (a+b+1)/2); scalar truncates.
 * Difference is 1 iff (a^b) has its LSB set, i.e. exactly one of {a,b}
 * is odd. Subtract the LSB-of-XOR to correct. */
__attribute__((target("avx2,fma")))
static inline __m256i debayer_avg2_u16_truncate(__m256i a, __m256i b, __m256i one_u16)
{
    __m256i avg = _mm256_avg_epu16(a, b);
    __m256i parity = _mm256_and_si256(_mm256_xor_si256(a, b), one_u16);
    return _mm256_sub_epi16(avg, parity);
}

/* Per-row debayer body for two consecutive RGB output rows starting at
 * source row index Y/width. Y is a flat pixel offset. The SIMD bulk
 * processes 16 source columns per inner iter starting at x_anchor=1. */
__attribute__((target("avx2,fma")))
static void debayer_basic_u16_rows_avx2(uint16_t * __restrict debayerto,
                                        uint16_t * __restrict bayerdata,
                                        int width,
                                        int Y)
{
    const int widthDB = width - 1;
    const int nextRowRGB = width * 3;
    const __m256i one_u16 = _mm256_set1_epi16(1);

    /* Per-iter SIMD covers x_anchor = 1, 17, 33, ... up to the last full
     * 16-column-wide chunk that ends at or before x_anchor + 15 < widthDB
     * (i.e., x_anchor + 15 <= widthDB - 1), so x_anchor <= widthDB - 16. */
    const int x_simd_last = widthDB - 16;

    {
        const uint16_t * row_m1 = bayerdata + (Y - width);
        const uint16_t * row_0  = bayerdata + Y;
        const uint16_t * row_p1 = bayerdata + (Y + width);
        const uint16_t * row_p2 = bayerdata + (Y + 2 * width);

        /* Output base for top RGB row at this iter = Y * 3.
         * For lane k of a 16-lane vector starting at x_anchor, the output
         * RGB pixel is at debayerto[(Y + x_anchor + k) * 3 .. +2]. */

        int x = 1;
        uint16_t __attribute__((aligned(32))) tR[16], tG[16], tB[16];
        uint16_t __attribute__((aligned(32))) bR[16], bG[16], bB[16];
        if (x <= x_simd_last)
        {
            for (; x <= x_simd_last; x += 16)
            {
                /* Three loads per row covering source cols x-1..x+15,
                 * x..x+16, x+1..x+17. Rows m1, 0, p1, p2. */
                __m256i s_m1_a = _mm256_loadu_si256((const __m256i*)(row_m1 + x - 1));
                __m256i s_m1_b = _mm256_loadu_si256((const __m256i*)(row_m1 + x));
                __m256i s_m1_c = _mm256_loadu_si256((const __m256i*)(row_m1 + x + 1));
                __m256i s_0_a  = _mm256_loadu_si256((const __m256i*)(row_0  + x - 1));
                __m256i s_0_b  = _mm256_loadu_si256((const __m256i*)(row_0  + x));
                __m256i s_0_c  = _mm256_loadu_si256((const __m256i*)(row_0  + x + 1));
                __m256i s_p1_a = _mm256_loadu_si256((const __m256i*)(row_p1 + x - 1));
                __m256i s_p1_b = _mm256_loadu_si256((const __m256i*)(row_p1 + x));
                __m256i s_p1_c = _mm256_loadu_si256((const __m256i*)(row_p1 + x + 1));
                __m256i s_p2_a = _mm256_loadu_si256((const __m256i*)(row_p2 + x - 1));
                __m256i s_p2_b = _mm256_loadu_si256((const __m256i*)(row_p2 + x));
                __m256i s_p2_c = _mm256_loadu_si256((const __m256i*)(row_p2 + x + 1));

                /* Top row stream (lanes alternate TL, TR; even=TL, odd=TR). */
                /* TL.R = avg4(m1[k-1], m1[k+1], p1[k-1], p1[k+1])
                 * TR.R = avg2(m1[k],   p1[k]) at lane k odd */
                __m256i v_TL_R = debayer_avg4_u16_truncate(s_m1_a, s_m1_c, s_p1_a, s_p1_c);
                __m256i v_TR_R = debayer_avg2_u16_truncate(s_m1_b, s_p1_b, one_u16);
                /* Blend: even lanes (mask bit=0) take v_TL_R, odd (bit=1) take v_TR_R.
                 * _mm256_blend_epi16 mask is per-128bit-lane 8-bit, so 0xAA selects
                 * lanes 1,3,5,7 from the second arg (and same in upper half). */
                __m256i top_R = _mm256_blend_epi16(v_TL_R, v_TR_R, 0xAA);

                /* TL.G = avg4(m1[k], 0[k+1], 0[k-1], p1[k])
                 * TR.G = 0[k] = s_0_b */
                __m256i v_TL_G = debayer_avg4_u16_truncate(s_m1_b, s_0_c, s_0_a, s_p1_b);
                __m256i top_G  = _mm256_blend_epi16(v_TL_G, s_0_b, 0xAA);

                /* TL.B = 0[k] = s_0_b
                 * TR.B = avg2(0[k-1], 0[k+1]) = avg2(s_0_a, s_0_c) */
                __m256i v_TR_B = debayer_avg2_u16_truncate(s_0_a, s_0_c, one_u16);
                __m256i top_B  = _mm256_blend_epi16(s_0_b, v_TR_B, 0xAA);

                /* Bot row stream (lanes alternate BL, BR; even=BL, odd=BR). */
                /* BL.R = avg2(p1[k-1], p1[k+1]) = avg2(s_p1_a, s_p1_c)
                 * BR.R = p1[k] = s_p1_b */
                __m256i v_BL_R = debayer_avg2_u16_truncate(s_p1_a, s_p1_c, one_u16);
                __m256i bot_R  = _mm256_blend_epi16(v_BL_R, s_p1_b, 0xAA);

                /* BL.G = p1[k] = s_p1_b
                 * BR.G = avg4(0[k], p1[k-1], p1[k+1], p2[k]) */
                __m256i v_BR_G = debayer_avg4_u16_truncate(s_0_b, s_p1_a, s_p1_c, s_p2_b);
                __m256i bot_G  = _mm256_blend_epi16(s_p1_b, v_BR_G, 0xAA);

                /* BL.B = avg2(0[k], p2[k]) = avg2(s_0_b, s_p2_b)
                 * BR.B = avg4(0[k-1], 0[k+1], p2[k-1], p2[k+1]) */
                __m256i v_BL_B = debayer_avg2_u16_truncate(s_0_b, s_p2_b, one_u16);
                __m256i v_BR_B = debayer_avg4_u16_truncate(s_0_a, s_0_c, s_p2_a, s_p2_c);
                __m256i bot_B  = _mm256_blend_epi16(v_BL_B, v_BR_B, 0xAA);

                /* Spill to 16-uint16 scratch arrays (32-byte aligned) and
                 * scalar-interleave-write to RGB AoS-3 output. The scalar
                 * loop is a tight stride-3 store that GCC autovec's into
                 * 128-bit pshufb-based interleaved stores. */
                _mm256_store_si256((__m256i*)tR, top_R);
                _mm256_store_si256((__m256i*)tG, top_G);
                _mm256_store_si256((__m256i*)tB, top_B);
                _mm256_store_si256((__m256i*)bR, bot_R);
                _mm256_store_si256((__m256i*)bG, bot_G);
                _mm256_store_si256((__m256i*)bB, bot_B);

                uint16_t * out_top = debayerto + (Y + x) * 3;
                uint16_t * out_bot = debayerto + (Y + width + x) * 3;
                for (int k = 0; k < 16; ++k)
                {
                    out_top[k * 3 + 0] = tR[k];
                    out_top[k * 3 + 1] = tG[k];
                    out_top[k * 3 + 2] = tB[k];
                    out_bot[k * 3 + 0] = bR[k];
                    out_bot[k * 3 + 1] = bG[k];
                    out_bot[k * 3 + 2] = bB[k];
                }
            }
        }

        /* Scalar tail for trailing inner cols. */
        for (; x < widthDB; x += 2)
        {
            int pix = Y + x;
            int pixm1 = pix - width;
            int pixp1 = pix + width;
            int pixp2 = pixp1 + width;

            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + 2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+2 ),
            };
            int rgbPix[4] = {
                (bPix[5] * 3), (bPix[ 6] * 3),
                (bPix[9] * 3), (bPix[10] * 3)
            };

            debayerto[ rgbPix[0] ] = (uint32_t)(
                  bayerdata[ bPix[0] ] + bayerdata[ bPix[ 2] ]
                + bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 2;
            debayerto[ rgbPix[0]+1 ] = (uint32_t)(
                  bayerdata[ bPix[1] ] + bayerdata[ bPix[6] ]
                + bayerdata[ bPix[4] ] + bayerdata[ bPix[9] ]
            ) >> 2;
            debayerto[ rgbPix[0]+2 ] = bayerdata[ bPix[5] ];
            debayerto[ rgbPix[1] ] = (uint32_t)(
                bayerdata[ bPix[2] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            debayerto[ rgbPix[1]+1 ] = bayerdata[ bPix[6] ];
            debayerto[ rgbPix[1]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[7] ]
            ) >> 1;
            debayerto[ rgbPix[2] ] = (uint32_t)(
                bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            debayerto[ rgbPix[2]+1 ] = bayerdata[ bPix[9] ];
            debayerto[ rgbPix[2]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[13] ]
            ) >> 1;
            debayerto[ rgbPix[3] ] = bayerdata[ bPix[10] ];
            debayerto[ rgbPix[3]+1 ] = (uint32_t)(
                  bayerdata[ bPix[ 6] ] + bayerdata[ bPix[ 9] ]
                + bayerdata[ bPix[11] ] + bayerdata[ bPix[14] ]
            ) >> 2;
            debayerto[ rgbPix[3]+2 ] = (uint32_t)(
                  bayerdata[ bPix[ 5] ] + bayerdata[ bPix[ 7] ]
                + bayerdata[ bPix[13] ] + bayerdata[ bPix[15] ]
            ) >> 2;
        }

        /* Edge fixups (identical to scalar). */
        uint16_t * edgePixel = debayerto + (3 * Y);
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        edgePixel += nextRowRGB;
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];
        edgePixel += nextRowRGB;
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];
    }
}

#endif /* DEBAYER_AVX2_AVAILABLE */

static int debayer_env_truthy(const char * v)
{
    if (!v || !*v) return 0;
    if (!strcmp(v, "1") || !strcmp(v, "true") || !strcmp(v, "TRUE")
     || !strcmp(v, "True") || !strcmp(v, "yes") || !strcmp(v, "on")) return 1;
    return 0;
}

static pthread_once_t g_debayer_basic_u16_dispatch_once = PTHREAD_ONCE_INIT;
static debayer_basic_u16_rows_fn_t g_debayer_basic_u16_rows_fn = NULL;
static int g_debayer_basic_u16_use_avx2 = 0;

static void debayer_basic_u16_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef DEBAYER_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (debayer_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (debayer_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DEBAYER"))) use_avx2 = 0;
    g_debayer_basic_u16_use_avx2 = use_avx2;
#ifdef DEBAYER_AVX2_AVAILABLE
    g_debayer_basic_u16_rows_fn = use_avx2 ? debayer_basic_u16_rows_avx2
                                           : debayer_basic_u16_rows_scalar;
#else
    g_debayer_basic_u16_rows_fn = debayer_basic_u16_rows_scalar;
#endif
}

int debayerBasicU16Avx2Active(void)
{
    pthread_once(&g_debayer_basic_u16_dispatch_once, debayer_basic_u16_dispatch_init);
    return g_debayer_basic_u16_use_avx2;
}

/* Test-only hook: force re-evaluation of the dispatch from current env.
 * Mirrors processingFastPathReinitDispatchForTesting. Not in the public
 * header. */
int debayerBasicU16ReinitDispatchForTesting(void);
int debayerBasicU16ReinitDispatchForTesting(void)
{
    pthread_once(&g_debayer_basic_u16_dispatch_once, debayer_basic_u16_dispatch_init);
    debayer_basic_u16_dispatch_init();
    return g_debayer_basic_u16_use_avx2;
}

void convert_to_log(void * data)
{
    // float
}

void debayerNoneU16(uint16_t * __restrict debayerto,
                    const uint16_t * __restrict bayerdata,
                    int width,
                    int height,
                    int threads,
                    int bit_shift)
{
    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < height; ++y)
    {
        const uint16_t * __restrict src_row = bayerdata + ((size_t)y * (size_t)width);
        uint16_t * __restrict dst_row = debayerto + ((size_t)y * (size_t)width * 3u);

        for (int x = 0; x < width; ++x)
        {
            const uint16_t value = (uint16_t)(src_row[x] << bit_shift);
            dst_row[0] = value;
            dst_row[1] = value;
            dst_row[2] = value;
            dst_row += 3;
        }
    }
}

void debayerBasicU16(uint16_t * __restrict debayerto,
                     uint16_t * __restrict bayerdata,
                     int width,
                     int height,
                     int threads,
                     int bit_shift)
{
    if (bit_shift > 0)
    {
        const int pixel_count = width * height;
        #pragma omp parallel for if(threads > 1) num_threads(threads)
        for (int i = 0; i < pixel_count; ++i)
        {
            bayerdata[i] = (uint16_t)(bayerdata[i] << bit_shift);
        }
    }

    /* Debayer pixel size (limit with 1 pixel border). */
    int pixelsizeDB;
    if( height % 2 == 0 )
        pixelsizeDB = width * (height - 1);
    else
        pixelsizeDB = width * (height - 2);

    const int step = width * 2;

    /* Latch the dispatch once, then divide the row-pair sweep across the
     * OMP thread pool with chunk size 1 so each thread inherits the
     * pthread_once-cached function pointer. */
    pthread_once(&g_debayer_basic_u16_dispatch_once, debayer_basic_u16_dispatch_init);
    debayer_basic_u16_rows_fn_t row_fn = g_debayer_basic_u16_rows_fn;

    /* Width too small for SIMD bulk: fall back to scalar full-row even on
     * AVX2 hosts. The 16-source-col SIMD step needs widthDB - 1 >= 16,
     * i.e. width >= 18. Anything less stays scalar to avoid bounds bugs. */
    if (width < 18)
    {
        row_fn = debayer_basic_u16_rows_scalar;
    }

    #pragma omp parallel for if(threads > 1) num_threads(threads) schedule(static)
    for (int Y = width; Y < pixelsizeDB; Y += step)
    {
        row_fn(debayerto, bayerdata, width, Y);
    }

    memcpy(debayerto, debayerto + (width * 3), width * 3 * sizeof(uint16_t));
    memcpy(debayerto + (width * (height - 1) * 3), debayerto + (width * (height - 2) * 3), width * 3 * sizeof(uint16_t));
}

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: horizontal Nyquist zero on a reduced recon's
 * debayered RGB16. Per channel, out = (l + 2c + r + 2) >> 2 along the row, with the
 * edge columns mirrored (x-1 -> x+1 at x=0, x+1 -> x-1 at x=w-1). The response is 0
 * at the 2-column period and 1 at DC. Rows and channels are independent; out and in
 * must not alias. The CUDA AMaZE backend's k_reduced_hnyquist121 is a line-for-line
 * port. */
void debayer_reduced_hnyquist121_rgb16(uint16_t *out, const uint16_t *in, int w, int h)
{
    if (!out || !in || w <= 0 || h <= 0) return;
    for (int y = 0; y < h; ++y)
    {
        const uint16_t *src = in + (size_t)y * (size_t)w * 3u;
        uint16_t *dst = out + (size_t)y * (size_t)w * 3u;
        for (int x = 0; x < w; ++x)
        {
            const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
            const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
            for (int c = 0; c < 3; ++c)
            {
                const uint32_t l = src[(size_t)xl * 3u + (size_t)c];
                const uint32_t m = src[(size_t)x * 3u + (size_t)c];
                const uint32_t r = src[(size_t)xr * 3u + (size_t)c];
                dst[(size_t)x * 3u + (size_t)c] = (uint16_t)((l + 2u * m + r + 2u) >> 2);
            }
        }
    }
}

typedef struct
{
    amazeinfo_t * info;
    int succeeded;
} debayer_amaze_thread_arg_t;

static int g_debayer_amaze_thread_create_failure_for_testing = -1;
static int g_debayer_amaze_allocation_failure_for_testing = 0;
static int g_debayer_amaze_aux_allocation_failure_for_testing = -1;

void debayerAmazeSetThreadCreateFailureForTesting(int thread_index)
{
    g_debayer_amaze_thread_create_failure_for_testing = thread_index;
}

void debayerAmazeSetAllocationFailureForTesting(int enabled)
{
    g_debayer_amaze_allocation_failure_for_testing = enabled != 0;
}

void debayerAmazeSetAuxAllocationFailureForTesting(int allocation_index)
{
    g_debayer_amaze_aux_allocation_failure_for_testing = allocation_index;
}

static void * debayer_amaze_aux_calloc(size_t count, size_t size, int allocation_index)
{
    if (g_debayer_amaze_aux_allocation_failure_for_testing == allocation_index)
        return NULL;
    return calloc(count, size);
}

static void * debayer_amaze_thread(void * opaque)
{
    debayer_amaze_thread_arg_t * arg = (debayer_amaze_thread_arg_t *)opaque;
    arg->succeeded = demosaic(arg->info);
    return NULL;
}

/* AmAZeMEmE debayer easier to use. Returns zero without publishing output if
 * any allocation or worker fails. */
int debayerAmaze(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads, int blacklevel)
{
    enum { DEBAYER_AMAZE_MAX_THREADS = 256 };
    if (!debayerto || !bayerdata || width < 33 || height < 33)
        return 0;
    const size_t checked_width = (size_t)width;
    const size_t checked_height = (size_t)height;
    if (checked_width > SIZE_MAX / checked_height)
        return 0;
    const size_t checked_pixels = checked_width * checked_height;
    if (checked_pixels > INT32_MAX
        || checked_pixels > SIZE_MAX / 3u
        || checked_pixels > SIZE_MAX / sizeof(float)
        || checked_height > SIZE_MAX / sizeof(float *))
    {
        return 0;
    }
    if (threads < 1)
        threads = 1;
    /* AMaZE needs at least 33 rows per worker.  Bound the caller-provided
     * count before it is used as a VLA extent or in chunk calculations. */
    const int geometry_thread_limit = height / 33;
    if (threads > geometry_thread_limit)
        threads = geometry_thread_limit;
    if (threads > DEBAYER_AMAZE_MAX_THREADS)
        threads = DEBAYER_AMAZE_MAX_THREADS;

    /* AmAZeMEmE wants an image as floating points and 2d arrey as well */
    const int force_allocation_failure = g_debayer_amaze_allocation_failure_for_testing;
    float ** __restrict imagefloat2d = force_allocation_failure ? NULL : (float **)malloc(checked_height * sizeof(float *));

    /* AmAZe also wants to return floats, so heres memeory 4 it */
    float  * __restrict red1d = force_allocation_failure ? NULL : (float *)malloc(checked_pixels * sizeof(float));
    float ** __restrict red2d = force_allocation_failure ? NULL : (float **)malloc(checked_height * sizeof(float *));
    float  * __restrict green1d = force_allocation_failure ? NULL : (float *)malloc(checked_pixels * sizeof(float));
    float ** __restrict green2d = force_allocation_failure ? NULL : (float **)malloc(checked_height * sizeof(float *));
    float  * __restrict blue1d = force_allocation_failure ? NULL : (float *)malloc(checked_pixels * sizeof(float));
    float ** __restrict blue2d = force_allocation_failure ? NULL : (float **)malloc(checked_height * sizeof(float *));
    if (!imagefloat2d || !red1d || !red2d || !green1d || !green2d || !blue1d || !blue2d)
    {
        free(red1d);
        free(red2d);
        free(green1d);
        free(green2d);
        free(blue1d);
        free(blue2d);
        free(imagefloat2d);
        return 0;
    }
    for (int y = 0; y < height; ++y)
    {
        imagefloat2d[y] = bayerdata + (size_t)y * checked_width;
        red2d[y] = red1d + (size_t)y * checked_width;
        green2d[y] = green1d + (size_t)y * checked_width;
        blue2d[y] = blue1d + (size_t)y * checked_width;
    }

    int succeeded = 1;

    /* If threads is < 2 just do a normal amaze */
    if (threads < 2)
    {
        /* run the Amaze */
        succeeded = demosaic( & (amazeinfo_t) {
                  imagefloat2d,
                  red2d,
                  green2d,
                  blue2d,
                  0, 0, /* crop window for demosaicing */
                  width, height,
                  0,
                  blacklevel} );
    }

    /* Else do multithreading */
    else
    {
        const size_t thread_count = (size_t)threads;
        int * startchunk_y = (int *)debayer_amaze_aux_calloc(thread_count, sizeof(int), 0);
        int * endchunk_y = (int *)debayer_amaze_aux_calloc(thread_count, sizeof(int), 1);
        pthread_t * thread_id = (pthread_t *)debayer_amaze_aux_calloc(thread_count, sizeof(pthread_t), 2);
        amazeinfo_t * amaze_arguments = (amazeinfo_t *)debayer_amaze_aux_calloc(thread_count, sizeof(amazeinfo_t), 3);
        debayer_amaze_thread_arg_t * thread_arguments =
            (debayer_amaze_thread_arg_t *)debayer_amaze_aux_calloc(
                thread_count, sizeof(debayer_amaze_thread_arg_t), 4);
        if (!startchunk_y || !endchunk_y || !thread_id || !amaze_arguments || !thread_arguments)
        {
            /* One cleanup owner below; leaving partial pointers intact avoids
             * the historical double-free on a later common cleanup. */
            succeeded = 0;
        }

        /* How big each thread's chunk is, multiple of 2 - or debayer
         * would start on wrong pixel and magenta stripes appear */
        int chunk_height = height / threads;
        chunk_height -= chunk_height % 2;

        /* To small chunk heights bring AMaZE module to crash */
        while( chunk_height <= 32 )
        {
            if( threads <= 1 ) break;
            threads--;
            chunk_height = height / threads;
            chunk_height -= chunk_height % 2;
        }

        /* Calculate chunks of image for each thread */
        for (int thread = 0; succeeded && thread < threads; ++thread)
        {
            startchunk_y[thread] = chunk_height * thread;
            endchunk_y[thread] = chunk_height * (thread + 1);
        }

        /* Last chunk must reach end of frame */
        if (succeeded)
            endchunk_y[threads-1] = height;
        int created_threads = 0;

        /* Create amaze pthreads */
        for (int thread = 0; succeeded && thread < threads; ++thread)
        {
            /* Amaze arguments */
            amaze_arguments[thread] = (amazeinfo_t) {
                imagefloat2d,
                red2d,
                green2d,
                blue2d,
                /* Crop out a part for each thread */
                0, startchunk_y[thread],    /* crop window for demosaicing */
                width, (endchunk_y[thread] - startchunk_y[thread]),
                0,
                blacklevel };

            thread_arguments[thread].info = &amaze_arguments[thread];
            thread_arguments[thread].succeeded = 0;
            const int create_result =
                g_debayer_amaze_thread_create_failure_for_testing == thread
                ? 1
                : pthread_create(&thread_id[thread],
                                 NULL,
                                 debayer_amaze_thread,
                                 &thread_arguments[thread]);
            if (create_result != 0)
            {
                succeeded = 0;
                break;
            }
            created_threads++;
        }

        /* let all threads finish */
        int workers_quiesced = 1;
        for (int thread = 0; thread < created_threads; ++thread)
        {
            if (pthread_join(thread_id[thread], NULL) != 0)
            {
                succeeded = 0;
                workers_quiesced = 0;
            }
            else if (!thread_arguments[thread].succeeded)
            {
                succeeded = 0;
            }
        }
        if (created_threads != threads)
            succeeded = 0;
        if (!workers_quiesced)
        {
            /* A worker whose termination is not established may still hold
             * these arguments and planes.  Leak rather than free live state. */
            return 0;
        }
        free(startchunk_y);
        free(endchunk_y);
        free(thread_id);
        free(amaze_arguments);
        free(thread_arguments);
    }

    //int rgb_pixels = pixelsize * 3;

    /* Give back as RGB only after every worker completed successfully. */
    if (succeeded)
    {
        for (size_t i = 0; i < checked_pixels; i++)
        {
            const size_t j = i * 3u;
            debayerto[ j ] = LIMIT16((uint32_t)red1d[i]);
            debayerto[j+1] = LIMIT16((uint32_t)green1d[i]);
            debayerto[j+2] = LIMIT16((uint32_t)blue1d[i]);
        }
    }

    free(red1d);
    free(red2d);
    free(green1d);
    free(green2d);
    free(blue1d);
    free(blue2d);
    free(imagefloat2d);
    return succeeded;
}



/* Quite quick bilinear debayer, floating point sadly; threads argument is unused */
void debayerBasic(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads)
{
    /* Hide warning */
    (void)threads;

    /* Debayer pixel size(limit with 1 pixel border to avoid seg fault, fix blank pixels L8ter) */
    int pixelsizeDB;
    /* when odd height, do less... */
    if( height % 2 == 0 )
        pixelsizeDB = width * (height - 1); /* How many pixels to go through in debayer (height - 1 to avoid bottom row) */
    else
        pixelsizeDB = width * (height - 2); /* How many pixels to go through in debayer (height - 2 to avoid bottom row) */
    int widthDB = width - 1; /* Debayering width */

    int step = width * 2; /* How many pixels to skip each time(2 rows worth) */
    int nextRowRGB = width * 3; /* Size of a row in colour, so it does not need to be calculated 1000x */

    /* Debayer main chunk, start 1 row in to avoid ze segfault :D */
    #pragma omp parallel for
    for (int Y = width; Y < pixelsizeDB; Y += step)
    {
        for (int x = 1; x < widthDB; x += 2) /* Stepping in rows */
        {
            /* Indexes of bayer pixels:
             *
             * R  G  R  G
             * G (B)(G) B
             * R (G)(R) G
             * G  B  G  B
             *
             * Middle 4 are current pixels we are working on */

            int pix = Y + x; /* Current pixel(RED) */
            int pixm1 = pix - width; /* Pixel of previous row by 1 */
            int pixp1 = pix + width; /* Next row pixel */
            int pixp2 = pixp1 + width; /* Row + 2 */

            /* Bayer pixel indexes */
            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + 2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+2 ),
            };

            /* Indexes of our four pixels in RGB(not every colour) */
            int rgbPix[4] = {
                (bPix[5] * 3), (bPix[ 6] * 3),
                (bPix[9] * 3), (bPix[10] * 3)
            };

            /* TOP LEFT pixel (BLUE on bayer) */
            /* Doing top left corner - RED on bayer */
            debayerto[ rgbPix[0] ] = (uint32_t)(
                  bayerdata[ bPix[0] ] + bayerdata[ bPix[ 2] ]
                + bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 2;
            /* GREEN */
            debayerto[ rgbPix[0]+1 ] = (uint32_t)(
                  bayerdata[ bPix[1] ] + bayerdata[ bPix[6] ]
                + bayerdata[ bPix[4] ] + bayerdata[ bPix[9] ]
            ) >> 2;
            /* BLUE */
            debayerto[ rgbPix[0]+2 ] = (uint16_t)bayerdata[ bPix[5] ]; /* Just BLUE - no DBAYERING needed */

            /* TOP RIGHT pixel (GREEN on bayer) */
            /* RED */
            debayerto[ rgbPix[1] ] = (uint32_t)(
                bayerdata[ bPix[2] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            /* GREEN */
            debayerto[ rgbPix[1]+1 ] = (uint16_t)bayerdata[ bPix[6] ];
            /* BLUE */
            debayerto[ rgbPix[1]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[7] ]
            ) >> 1;

            /* BOTTOM LEFT pixel (GREEN on bayer) */
            /* RED */
            debayerto[ rgbPix[2] ] = (uint32_t)(
                bayerdata[ bPix[8] ] + bayerdata[ bPix[10] ]
            ) >> 1;
            /* GREEN */
            debayerto[ rgbPix[2]+1 ] = (uint16_t)bayerdata[ bPix[9] ];
            /* BLUE */
            debayerto[ rgbPix[2]+2 ] = (uint32_t)(
                bayerdata[ bPix[5] ] + bayerdata[ bPix[13] ]
            ) >> 1;

            /* BOTTOM RIGHT pixel (RED on bayer) */
            /* RED */
            debayerto[ rgbPix[3] ] = (uint16_t)bayerdata[ bPix[10] ];
            /* GREEN */
            debayerto[ rgbPix[3]+1 ] = (uint32_t)(
                  bayerdata[ bPix[ 6] ] + bayerdata[ bPix[ 9] ]
                + bayerdata[ bPix[11] ] + bayerdata[ bPix[14] ]
            ) >> 2;
            /* BLUE */
            debayerto[ rgbPix[3]+2 ] = (uint32_t)(
                  bayerdata[ bPix[ 5] ] + bayerdata[ bPix[ 7] ]
                + bayerdata[ bPix[13] ] + bayerdata[ bPix[15] ]
            ) >> 2;
        }

        /* Fix broken pixels at the edges by copying from the ones next to them */
        uint16_t * edgePixel = debayerto + (3 * Y); /* So we don't need more calculating later */
        /* Now fix them */
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        /* Move pointer one row along */
        edgePixel += nextRowRGB;
        /* Fix left pixel */
        edgePixel[0] = edgePixel[3];
        edgePixel[1] = edgePixel[4];
        edgePixel[2] = edgePixel[5];
        /* Fix right pixel (comes just before left 1) */
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];
        /* Move pointer one row along */
        edgePixel += nextRowRGB;
        /* Fix last right pixel */
        edgePixel[-1] = edgePixel[-4];
        edgePixel[-2] = edgePixel[-5];
        edgePixel[-3] = edgePixel[-6];

    }

    /* Copy to top/bottom rows */
    memcpy(debayerto, debayerto + (width * 3), width * 3 * sizeof(uint16_t));
    memcpy(debayerto + (width * (height - 1) * 3), debayerto + (width * (height - 2) * 3), width * 3 * sizeof(uint16_t));

}

/* Simple debayer single thread: one RGB pixel is 2x2 RAW pixels */
void debayerSimpleThread( easydebayerinfo_t * data )
{
    /* single lines can't be handled */
    if( data->height % 2 ) data->height--;

    int start = data->width * data->offsetY;
    int end = data->width * data->height;
    int pixelSkipR = 3 * data->width;
    int pixelSkipB = 3 * data->width - 2;

    for( int i = start, o = start*3; i < end; i++, o+=3 )
    {
        /* copy colors always to the whole 2x2 pixel */
        if( (i % 2) == 0 && ( ( i / data->width ) % 2 ) == 0 ) //R
        {
            data->debayerto[o] = (uint16_t)data->bayerdata[i];
            data->debayerto[o+3] = (uint16_t)data->bayerdata[i]; // +1pixel
            data->debayerto[o+pixelSkipR] = (uint16_t)data->bayerdata[i]; // +1line
            data->debayerto[o+pixelSkipR+3] = (uint16_t)data->bayerdata[i]; // +1line +1pixel
        }
        else if( (i % 2) == 1 && ( ( i / data->width ) % 2 ) == 1 ) //B
        {
            data->debayerto[o+2] = (uint16_t)data->bayerdata[i];
            data->debayerto[o-1] = (uint16_t)data->bayerdata[i]; // -1pixel
            data->debayerto[o-pixelSkipB] = (uint16_t)data->bayerdata[i]; // -1line
            data->debayerto[o-pixelSkipB-3] = (uint16_t)data->bayerdata[i]; // -1line -1pixel
        }
        else //G
        {
            data->debayerto[o+1] = (uint16_t)data->bayerdata[i];
            if( (i % 2) == 1 ) data->debayerto[o-2] = (uint16_t)data->bayerdata[i]; // -1pixel
            else data->debayerto[o+4] = (uint16_t)data->bayerdata[i]; // +1pixel
        }
    }
}

/* no debayer single thread, just copy some bytes to somewhere else :-P */
void debayerNoneThread( easydebayerinfo_t * data )
{
    int start = data->width * data->offsetY;
    int end = data->width * data->height;

    for( int i = start, o = start*3; i < end; i++, o+=3 )
    {
        /* no idea what I do here, but I get a B/W picture */
        data->debayerto[o] = (uint16_t)data->bayerdata[i];
        data->debayerto[o+1] = (uint16_t)data->bayerdata[i];
        data->debayerto[o+2] = (uint16_t)data->bayerdata[i];
    }
}

/* easy debayer types, threaded */
void debayerEasy(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads, int type)
{
    /* If threads is < 2 just do it normal */
    if (threads < 2)
    {
        if( type == 2 ) debayerNoneThread( & (easydebayerinfo_t) { debayerto, bayerdata, width, height, 0 } );
        else debayerSimpleThread( & (easydebayerinfo_t) { debayerto, bayerdata, width, height, 0 } );
    }
    else
    {
        int startchunk_y[threads];
        int endchunk_y[threads];

        /* How big each thread's chunk is, multiple of 2 - or debayer
         * would start on wrong pixel and magenta stripes appear */
        int chunk_height = height / threads;
        chunk_height -= chunk_height % 2;

        /* Calculate chunks of image for each thread */
        for (int thread = 0; thread < threads; ++thread)
        {
            startchunk_y[thread] = chunk_height * thread;
            endchunk_y[thread] = chunk_height * (thread + 1);
        }

        /* Last chunk must reach end of frame */
        endchunk_y[threads-1] = height;

        pthread_t thread_id[threads];
        easydebayerinfo_t none_arguments[threads];

        /* Create pthreads */
        for (int thread = 0; thread < threads; ++thread)
        {
            /* Amaze arguments */
            none_arguments[thread] = (easydebayerinfo_t) {
                debayerto,
                bayerdata,
                /* Crop out a part for each thread */
                width,
                endchunk_y[thread],
                startchunk_y[thread] };

            /* Create pthread! */
            if( type == 2 ) pthread_create( &thread_id[thread], NULL, (void *)&debayerNoneThread, (void *)&none_arguments[thread] );
            else pthread_create( &thread_id[thread], NULL, (void *)&debayerSimpleThread, (void *)&none_arguments[thread] );
        }

        /* let all threads finish */
        for (int thread = 0; thread < threads; ++thread)
        {
            pthread_join( thread_id[thread], NULL );
        }
    }
}

void debayerLibRtProcess(uint16_t *debayerto, float *bayerdata, int width, int height, int algorithm, double camMatrix[9])
{
    int pixelsize = width * height;

    /* lrtp wants an image as floating points and 2d arrey as well */
    float ** __restrict imagefloat2d = (float **)malloc(height * sizeof(float *));
    for (int y = 0; y < height; ++y) imagefloat2d[y] = (float *)(bayerdata+(y*width));

    /* lrtp also wants to return floats, so heres memeory 4 it */
    float  * __restrict red1d = (float *)malloc(pixelsize * sizeof(float));
    float ** __restrict red2d = (float **)malloc(height * sizeof(float *));
    for (int y = 0; y < height; ++y) red2d[y] = (float *)(red1d+(y*width));
    float  * __restrict green1d = (float *)malloc(pixelsize * sizeof(float));
    float ** __restrict green2d = (float **)malloc(height * sizeof(float *));
    for (int y = 0; y < height; ++y) green2d[y] = (float *)(green1d+(y*width));
    float  * __restrict blue1d = (float *)malloc(pixelsize * sizeof(float));
    float ** __restrict blue2d = (float **)malloc(height * sizeof(float *));
    for (int y = 0; y < height; ++y) blue2d[y] = (float *)(blue1d+(y*width));

    if( algorithm == 4)
        lrtpLmmseDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height );
    else if( algorithm == 5 )
        lrtpIgvDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height );
    else if( algorithm == 6 )
        lrtpAhdDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height, camMatrix );
    else if( algorithm == 7 )
        lrtpRcdDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height );
    else if( algorithm == 8 )
        lrtpDcbDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height );
    else //AMaZE
        lrtpAmazeDemosaic( imagefloat2d, red2d, green2d, blue2d, width, height );

    //int rgb_pixels = pixelsize * 3;

    /* Giv back as RGB, not separate channels */
    for (int i = 0; i < pixelsize; i++)
    {
        int j = i * 3;
        debayerto[ j ] = LIMIT16((uint32_t)red1d[i]);
        debayerto[j+1] = LIMIT16((uint32_t)green1d[i]);
        debayerto[j+2] = LIMIT16((uint32_t)blue1d[i]);
    }

    free(red1d);
    free(red2d);
    free(green1d);
    free(green2d);
    free(blue1d);
    free(blue2d);
    free(imagefloat2d);
}
