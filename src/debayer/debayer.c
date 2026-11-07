#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
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
            /* At an odd width the last block's x+2 is past the row (one word past
             * the frame on the final row pair). Mirror it onto x, the same CFA
             * colour; it only feeds column x+1, which the edge fix-up overwrites. */
            const int r2 = (x + 2 < width) ? 2 : 0;

            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+r2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + r2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+r2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+r2 ),
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
            /* Odd-width right edge: as in the scalar body. */
            const int r2 = (x + 2 < width) ? 2 : 0;

            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+r2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + r2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+r2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+r2 ),
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

/* CPU-DUALISO-REDUCED-RECON-H2C-PORT-1: the chroma-only [1,2,1]/4 along x (see
 * debayer.h). The x4 CPU reduced route runs it on its x2 debayer before the 2x2 box
 * ("H2c"; the x2 route does not, r8): it lowpasses R-G and B-G across the
 * columns, where the shrunk Bayer's colour sites sit a full reduced pixel apart, and
 * leaves G (the picture's sharpness) alone. */
/* sel (r12 pass 2 A1): NULL, or the per-frame fit slopes {aR, aB} plus keep bits (sel_keep). For a channel whose fit
 * was usable, the taps' own curvature |R_l - 2 R_m + R_r| / a is compared with the curvature of the balanced colour
 * difference |(R_l - 2 R_m + R_r) / a - (G_l - 2 G_m + G_r)|; where the channel itself is the smoother one (an R or B
 * that does not follow G, e.g. affine under a G-only step) it takes the plain [1 2 1] of itself, which is exact on an
 * affine channel, instead of laying the colour difference on G. */
/* r14 (pace): the colour-difference value of one channel c (0 R, 2 B): the taps' mean colour difference laid on the
 * centre G, clipped to 16 bits and, ranged, to [min, max] of the channel's own three taps. */
static inline int debayer_hchroma121_cd(const uint16_t *pl, const uint16_t *pm, const uint16_t *pr, int c, int ranged)
{
    const int g = (int)pm[1];
    const int s = ((int)pl[c] - (int)pl[1]) + 2 * ((int)pm[c] - g) + ((int)pr[c] - (int)pr[1]);
    int v = g + ((s + 2) >> 2);
    if (v < 0) v = 0;
    if (v > 65535) v = 65535;
    if (ranged)
    {
        const int lo = MIN(MIN((int)pl[c], (int)pm[c]), (int)pr[c]);
        const int hi = MAX(MAX((int)pl[c], (int)pm[c]), (int)pr[c]);
        if (v < lo) v = lo;
        else if (v > hi) v = hi;
    }
    return v;
}

/* r14 (pace): the r12 pass 2 A1 own-path test over a tile, L the channel's and G the G curvature per pixel, a > 0 the
 * fit slope. r12 took |(float)L| / a <= |(float)L / a - (float)G| per pixel inside a branch; |L| / a == |L / a| exactly
 * for a > 0, so this is the same IEEE test on one quotient. Written as a straight loop over plain arrays so the
 * compiler can run it four lanes wide (divps / cmpleps round and compare each lane as divss / comiss do), with no
 * data-dependent branch. */
#define DEBAYER_H121_TILE 256
static void debayer_hchroma121_own_tile(int *own, const int *L, const int *G, int n, float a)
{
    for (int i = 0; i < n; ++i)
    {
        const float q = (float)L[i] / a;
        own[i] = fabsf(q) <= fabsf(q - (float)G[i]);
    }
}

static void debayer_reduced_hchroma121_impl(uint16_t *out, const uint16_t *in, int w, int h, int threads, int ranged,
                                            const float *sel, int sel_keep)
{
    if (!out || !in || w <= 0 || h <= 0) return;
    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < h; ++y)
    {
        const uint16_t *src = in + (size_t)y * (size_t)w * 3u;
        uint16_t *dst = out + (size_t)y * (size_t)w * 3u;
        if (!sel)
        {
            for (int x = 0; x < w; ++x)
            {
                const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
                const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
                const uint16_t *pl = src + (size_t)xl * 3u, *pm = src + (size_t)x * 3u, *pr = src + (size_t)xr * 3u;
                dst[(size_t)x * 3u + 0u] = (uint16_t)debayer_hchroma121_cd(pl, pm, pr, 0, ranged);
                dst[(size_t)x * 3u + 2u] = (uint16_t)debayer_hchroma121_cd(pl, pm, pr, 2, ranged);
                dst[(size_t)x * 3u + 1u] = pm[1];
            }
            continue;
        }
        /* sel: tiles of the row; curvatures, then the vector test, then a select per pixel */
        for (int x0 = 0; x0 < w; x0 += DEBAYER_H121_TILE)
        {
            const int n = MIN(DEBAYER_H121_TILE, w - x0);
            int L[2][DEBAYER_H121_TILE], Gc[DEBAYER_H121_TILE], own[2][DEBAYER_H121_TILE];
            for (int i = 0; i < n; ++i)
            {
                const int x = x0 + i;
                const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
                const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
                const uint16_t *pl = src + (size_t)xl * 3u, *pm = src + (size_t)x * 3u, *pr = src + (size_t)xr * 3u;
                Gc[i] = (int)pl[1] - 2 * (int)pm[1] + (int)pr[1];
                L[0][i] = (int)pl[0] - 2 * (int)pm[0] + (int)pr[0];
                L[1][i] = (int)pl[2] - 2 * (int)pm[2] + (int)pr[2];
            }
            for (int ci = 0; ci < 2; ++ci)
            {
                /* r12 pass 3 A1: a kept channel (degenerate fit, e.g. a negative slope) is the bilinear's, so it takes
                 * its own [1 2 1] as well; the colour difference to G is never laid on it. */
                if (sel_keep & (1 << ci))
                    for (int i = 0; i < n; ++i) own[ci][i] = 1;
                else
                    debayer_hchroma121_own_tile(own[ci], L[ci], Gc, n, sel[ci]);
            }
            for (int i = 0; i < n; ++i)
            {
                const int x = x0 + i;
                const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
                const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
                const uint16_t *pl = src + (size_t)xl * 3u, *pm = src + (size_t)x * 3u, *pr = src + (size_t)xr * 3u;
                for (int c = 0; c < 3; c += 2)
                {
                    const int v = debayer_hchroma121_cd(pl, pm, pr, c, ranged);
                    const int vo = ((int)pl[c] + 2 * (int)pm[c] + (int)pr[c] + 2) >> 2;
                    dst[(size_t)x * 3u + (size_t)c] = (uint16_t)(own[c ? 1 : 0][i] ? vo : v);
                }
                dst[(size_t)x * 3u + 1u] = pm[1];
            }
        }
    }
}

void debayer_reduced_hchroma121_rgb16(uint16_t *out, const uint16_t *in, int w, int h, int threads)
{
    debayer_reduced_hchroma121_impl(out, in, w, h, threads, 0, NULL, 0);
}

/* r12 (adversary pass 9 A1, see debayer.h): the [1 2 1] lays the taps' mean colour
 * difference on the centre G, so a G-only edge under an R or B that HA-CD had held
 * constant came back through it (R = B = 9062 / 10937 for 10000 after the 2x2 box). */
void debayer_reduced_hchroma121_ranged_rgb16(uint16_t *out, const uint16_t *in, int w, int h, int threads)
{
    debayer_reduced_hchroma121_impl(out, in, w, h, threads, 1, NULL, 0);
}

/* CPU-DUALISO-REDUCED-RECON-H2C-PORT-1 r10 (see debayer.h). r9 failed V-FRINGE-OWN at
 * s4 slot 150 (0.949 > 0.90). The run dir's sim_r10b.py / sim_r10c.py model the x4
 * route against the base x4 route per phase: a row-wise HA G on the raw (gained)
 * Bayer doubles the fringe, because HA's Laplacian term assumes R, G and B share one
 * gain. Balanced, gradient-directed HA with the colour difference laid on the
 * bilinear G keeps every modelled phase at or under 0.89 of base (bilinear 1.14). */
static inline int debayer_hacd_reflect(int i, int n)
{
    if (i < 0) i = -i;
    if (i >= n) i = 2 * (n - 1) - i;
    return i;
}

/* Least squares v ~ a * g4 + c over the sites of one colour (row parity py, column
 * parity px, every second such row), g4 = the mean of the 4 G neighbours. Returns 1 for a
 * usable fit, 0 for a degenerate one (under 16 sites, no G variance, or a slope outside
 * [1/64, 64]); r11: the caller then keeps that channel's bilinear (adversary A1: a = 1,
 * c = 0 on a gained raw doubled a neutral edge's false colour against the bilinear). */
static int debayer_hacd_fit(const uint16_t *bayer, int w, int h, int py, int px, float *a_out, float *c_out)
{
    double n = 0.0, sg = 0.0, sv = 0.0, sgg = 0.0, sgv = 0.0;
    for (int y = 2 + py; y + 1 < h; y += 4)
    {
        const uint16_t *row = bayer + (size_t)y * (size_t)w;
        const uint16_t *up = row - w;
        const uint16_t *dn = row + w;
        for (int x = (px ? 1 : 2); x + 1 < w; x += 2)
        {
            const double g = ((double)up[x] + (double)dn[x] + (double)row[x - 1] + (double)row[x + 1]) * 0.25;
            const double v = (double)row[x];
            n += 1.0;
            sg += g;
            sv += v;
            sgg += g * g;
            sgv += g * v;
        }
    }
    float a = 1.0f, c = 0.0f;
    int ok = 0;
    if (n >= 16.0)
    {
        const double mg = sg / n, mv = sv / n;
        const double var = sgg / n - mg * mg;
        const double cov = sgv / n - mg * mv;
        if (var > 1.0)
        {
            const double fa = cov / var;
            if (fa >= 1.0 / 64.0 && fa <= 64.0)
            {
                a = (float)fa;
                c = (float)(mv - fa * mg);
                ok = 1;
            }
        }
    }
    *a_out = a;
    *c_out = c;
    return ok;
}

/* r14 (pace): v clamped to [min(a, b), max(a, b)], inline. It is the value fminf(fmaxf(v, min), max) gives
 * (r11, debayer_hacd_site) for the finite operands the site has: a and b are G samples (>= +0, never -0), so the
 * two forms cannot differ even in the sign of a zero. Without -ffast-math GCC emits fminf / fmaxf as libm calls,
 * eight per R / B site, a large share of the x4 chroma step's cost (r13 bench). */
static inline float debayer_hacd_clamp2(float v, float a, float b)
{
    const float lo = (a < b) ? a : b, hi = (a < b) ? b : a;
    return (v < lo) ? lo : ((v > hi) ? hi : v);
}

static inline uint16_t debayer_hacd_round16(float v)
{
    if (!(v > 0.0f)) return 0;
    if (v >= 65535.0f) return 65535;
    return (uint16_t)(int)(v + 0.5f);
}

/* One R or B site: its balanced value minus the gradient-directed HA G (row or
 * column, the smaller |dG| + |Laplacian|; a tie averages both), G clamped to
 * [0, 65535]. xl1 .. xr2 are the (mirrored) columns x - 1, x + 1, x - 2, x + 2. */
static inline float debayer_hacd_site(const uint16_t *row, const uint16_t *u1, const uint16_t *d1,
                                      const uint16_t *u2, const uint16_t *d2, int x, int xl1, int xr1,
                                      int xl2, int xr2, float ck, float ik)
{
#define HACD_BAL(v) (((float)(v) - ck) * ik)
    const float c = HACD_BAL(row[x]);
    const float l1 = (float)row[xl1], r1 = (float)row[xr1];
    const float l2 = HACD_BAL(row[xl2]), r2 = HACD_BAL(row[xr2]);
    const float up1 = (float)u1[x], dn1 = (float)d1[x];
    const float lh = 2.0f * c - l2 - r2;
    const float lv = 2.0f * c - HACD_BAL(u2[x]) - HACD_BAL(d2[x]);
#undef HACD_BAL
    /* r11: each directional G is clamped to its two G neighbours. Unclamped, the Laplacian of a colour
     * whose difference to G is not constant (a saturated colour edge, or the a = 1, c = 0 fallback on a
     * gained raw) pushed G past both neighbours and put more false colour on the edge than the bilinear
     * (run dir sim_r11b.py / sim_r11c.py: red disc on blue 1.143x, fallback diagonal step 1.125x). */
    const float gh = debayer_hacd_clamp2(0.5f * (l1 + r1) + 0.25f * lh, l1, r1);
    const float gv = debayer_hacd_clamp2(0.5f * (up1 + dn1) + 0.25f * lv, up1, dn1);
    const float dh = fabsf(l1 - r1) + fabsf(lh);
    const float dv = fabsf(up1 - dn1) + fabsf(lv);
    float gg = (dh < dv) ? gh : (dv < dh) ? gv : 0.5f * (gh + gv);
    if (gg < 0.0f) gg = 0.0f;
    if (gg > 65535.0f) gg = 65535.0f;
    return c - gg;
}

/* One output pixel: the colour difference at its own site or the bilinear mean of its
 * nearest R / B sites (dif rows ro, uo = y - 1, dof = y + 1, columns xl, xr mirrored),
 * laid on the pixel's bilinear G and mapped back by the fit. keep: bit 0 keeps the
 * bilinear R, bit 1 the bilinear B (that channel's fit was degenerate, r11). */
static inline void debayer_hacd_px(uint16_t *out, const float *dif, size_t ro, size_t uo, size_t dof, int x,
                                   int xl, int xr, int ye, int xe, float aR, float cR, float aB, float cB, int keep)
{
#define HACD_D(o, xx) (dif[(o) + (size_t)(xx)])
    float dr, db;
    if (ye && xe)
    {   /* R site */
        dr = HACD_D(ro, x);
        db = 0.25f * (HACD_D(uo, xl) + HACD_D(uo, xr) + HACD_D(dof, xl) + HACD_D(dof, xr));
    }
    else if (!ye && !xe)
    {   /* B site */
        db = HACD_D(ro, x);
        dr = 0.25f * (HACD_D(uo, xl) + HACD_D(uo, xr) + HACD_D(dof, xl) + HACD_D(dof, xr));
    }
    else if (ye)
    {   /* G in an R row: R left and right, B above and below */
        dr = 0.5f * (HACD_D(ro, xl) + HACD_D(ro, xr));
        db = 0.5f * (HACD_D(uo, x) + HACD_D(dof, x));
    }
    else
    {   /* G in a B row: R above and below, B left and right */
        dr = 0.5f * (HACD_D(uo, x) + HACD_D(dof, x));
        db = 0.5f * (HACD_D(ro, xl) + HACD_D(ro, xr));
    }
#undef HACD_D
    uint16_t *p = out + (size_t)x * 3u;
    const float gb = (float)p[1];
    if (!(keep & 1)) p[0] = debayer_hacd_round16(aR * (gb + dr) + cR);
    if (!(keep & 2)) p[2] = debayer_hacd_round16(aB * (gb + db) + cB);
}

/* r12 (adversary r12 pass 2 A1): colour-difference interpolation is exact where the balanced colour difference is
 * locally affine, the plain bilinear where the channel itself is. An R / B that does not follow G (affine under a
 * G-only step: 6568 for 6650 after the box) took G's edge. At the pixel's nearest own site s (mirrored), the
 * channel's own curvature |v(s-2) - 2 v(s) + v(s+2)| along x plus along y, divided by the fit slope a, is compared
 * with the same curvature of the colour difference (dif, written at every own site); where the channel is not the
 * rougher one the pixel takes the bilinear mean of its own nearest samples (exact on an affine channel) instead.
 * Reads dif only at own sites. A kept (bilinear) channel is not touched. */
static void debayer_hacd_select_row(uint16_t *out, const uint16_t *bayer, const float *dif, int w, int h, int y,
                                    int keep, float aR, float aB)
{
#define HACD_I(yy, xx) ((int)bayer[(size_t)(yy) * (size_t)w + (size_t)(xx)])
#define HACD_F(yy, xx) (dif[(size_t)(yy) * (size_t)w + (size_t)(xx)])
    for (int ch = 0; ch < 2; ++ch)
    {
        if (keep & (1 << ch)) continue;
        const float a = ch ? aB : aR;
        const int py = ch; /* own rows / columns: R even, B odd */
        int sy = (y & ~1) | py;
        if (sy >= h) sy -= 2;
        const int ya = debayer_hacd_reflect(sy - 2, h), yb = debayer_hacd_reflect(sy + 2, h);
        const int oy = ((y & 1) == py); /* the pixel is on an own row */
        const int yo1 = debayer_hacd_reflect(y - 1, h), yo2 = debayer_hacd_reflect(y + 1, h);
        /* r14 (pace): the test depends on the own site sx = 2 j + py alone, which two (at a clamped odd edge, three)
         * adjacent pixels share, so it runs once per site, in tiles: the curvature sums first, then r12's float test
         * (own sum / a <= cd, the same IEEE divide and compare) as a straight loop the compiler can run four lanes
         * wide, then each pixel picks its value with a select instead of a data-dependent branch. */
        const int ns = (w - py + 1) / 2; /* own sites sx = 2 j + py < w */
        for (int j0 = 0; j0 < ns; j0 += DEBAYER_H121_TILE)
        {
            const int nj = MIN(DEBAYER_H121_TILE, ns - j0);
            float so[DEBAYER_H121_TILE], sc[DEBAYER_H121_TILE];
            int take[DEBAYER_H121_TILE];
            for (int t = 0; t < nj; ++t)
            {
                const int sx = 2 * (j0 + t) + py;
                const int xa = debayer_hacd_reflect(sx - 2, w), xb = debayer_hacd_reflect(sx + 2, w);
                /* integer: r12's float sum of two |curvatures| of 16-bit samples is exact (< 2^24) */
                so[t] = (float)(abs(HACD_I(sy, xa) - 2 * HACD_I(sy, sx) + HACD_I(sy, xb))
                              + abs(HACD_I(ya, sx) - 2 * HACD_I(sy, sx) + HACD_I(yb, sx)));
                sc[t] = fabsf(HACD_F(sy, xa) - 2.0f * HACD_F(sy, sx) + HACD_F(sy, xb))
                      + fabsf(HACD_F(ya, sx) - 2.0f * HACD_F(sy, sx) + HACD_F(yb, sx));
            }
            for (int t = 0; t < nj; ++t) take[t] = so[t] / a <= sc[t];
            /* pixel x's site is j = x >> 1, except past the last site (odd w, B), which mirrors to j - 1 */
            const int x1 = (j0 + nj == ns) ? w : 2 * (j0 + nj);
            for (int x = 2 * j0; x < x1; ++x)
            {
                int t = (x >> 1) - j0;
                if (t >= nj) t = nj - 1;
                const int ox = ((x & 1) == py);
                const int xo1 = debayer_hacd_reflect(x - 1, w), xo2 = debayer_hacd_reflect(x + 1, w);
                /* integer: r12's debayer_hacd_round16 of the float mean of 1, 2 or 4 16-bit samples is exact here (the
                 * sums are under 2^24, the halves and quarters exact), so it equals the rounded integer mean */
                int nv;
                if (oy && ox) nv = HACD_I(y, x);
                else if (oy) nv = (HACD_I(y, xo1) + HACD_I(y, xo2) + 1) >> 1;
                else if (ox) nv = (HACD_I(yo1, x) + HACD_I(yo2, x) + 1) >> 1;
                else nv = (HACD_I(yo1, xo1) + HACD_I(yo1, xo2) + HACD_I(yo2, xo1) + HACD_I(yo2, xo2) + 2) >> 2;
                uint16_t *po = out + (size_t)x * 3u + (ch ? 2u : 0u);
                *po = take[t] ? (uint16_t)nv : *po;
            }
        }
    }
#undef HACD_I
#undef HACD_F
}

/* r11 (adversary pass 2 A1): each rebuilt R / B is clamped to [min, max] of that channel's own Bayer
 * samples in the pixel's 5x5 window (mirrored; a mirror keeps the CFA parity). With a valid fit, a G-only
 * edge under a constant R or B imprinted the G edge onto it where the bilinear is exact (run dir
 * sim_r11f.py: R(48,23) 10000 -> 8383); a channel constant over the window is now exact, and a neutral
 * edge's true value stays inside the range (modelled false colour falls further, e.g. diagonal step
 * 0.500 -> 0.343 of the bilinear). Rows are independent; a kept (bilinear) channel is not touched. */
/* r14 (pace): min / max over one own column s (mirrored) of the window's own rows (k0, k0 + 2 and, on an own row,
 * k0 + 4). The window is separable: a pixel on an own column takes columns x - 2, x, x + 2, any other pixel
 * x - 1, x + 1, so each column is reduced once and shared by the three pixels whose window holds it. */
static inline void debayer_hacd_colrange(const uint16_t *const *rows, int k0, int w, int s, int *lo, int *hi)
{
    if (s < 0 || s >= w) s = debayer_hacd_reflect(s, w);
    const int a = rows[k0][s], b = rows[k0 + 2][s];
    int l = (a < b) ? a : b, u = (a < b) ? b : a;
    if (k0 == 0)
    {
        const int c = rows[4][s];
        if (c < l) l = c;
        if (c > u) u = c;
    }
    *lo = l;
    *hi = u;
}

static inline void debayer_hacd_range_clamp(uint16_t *p, int lo, int hi)
{
    if ((int)*p < lo) *p = (uint16_t)lo;
    else if ((int)*p > hi) *p = (uint16_t)hi;
}

static void debayer_hacd_range_row(uint16_t *out, const uint16_t *bayer, int w, int h, int y, int keep)
{
    const uint16_t *rows[5];
    for (int k = 0; k < 5; ++k)
        rows[k] = bayer + (size_t)debayer_hacd_reflect(y - 2 + k, h) * (size_t)w;
    for (int ch = 0; ch < 2; ++ch)
    {
        if (keep & (1 << ch)) continue;
        const int k0 = ((y & 1) == ch) ? 0 : 1; /* R (ch 0) on even rows and columns, B (ch 1) on odd */
        uint16_t *o = out + (ch ? 2u : 0u);
        int x = 0, al, ah, bl, bh, cl, chi;
        if (ch)
        {   /* x = 0 is not a B column: its own columns -1 and 1 are both column 1 */
            debayer_hacd_colrange(rows, k0, w, 1, &bl, &bh);
            debayer_hacd_range_clamp(o, bl, bh);
            x = 1;
        }
        /* x is now an own column; a = column x - 2, b = column x, c = column x + 2 */
        debayer_hacd_colrange(rows, k0, w, x - 2, &al, &ah);
        debayer_hacd_colrange(rows, k0, w, x, &bl, &bh);
        for (; x < w; x += 2)
        {
            debayer_hacd_colrange(rows, k0, w, x + 2, &cl, &chi);
            const int ml = (bl < cl) ? bl : cl, mh = (bh > chi) ? bh : chi;
            debayer_hacd_range_clamp(o + (size_t)x * 3u, (al < ml) ? al : ml, (ah > mh) ? ah : mh);
            if (x + 1 < w) debayer_hacd_range_clamp(o + (size_t)(x + 1) * 3u, ml, mh);
            al = bl; ah = bh;
            bl = cl; bh = chi;
        }
    }
}

static int debayer_hacd_impl(uint16_t *rgb, const uint16_t *bayer, int w, int h, int threads, float *scratch,
                             float *sel_out, int *keep_out)
{
    if (!rgb || !bayer || !scratch || w < 4 || h < 4) return 0;
    float aR, cR, aB, cB;
    const int okR = debayer_hacd_fit(bayer, w, h, 0, 0, &aR, &cR); /* R at (even, even) */
    const int okB = debayer_hacd_fit(bayer, w, h, 1, 1, &aB, &cB); /* B at (odd, odd) */
    /* r11: a degenerate fit keeps that channel as debayerBasicU16 wrote it. */
    const int keep = (okR ? 0 : 1) | (okB ? 0 : 2);
    if (sel_out) { sel_out[0] = aR; sel_out[1] = aB; }
    if (keep_out) *keep_out = keep;
    if (keep == 3) return 1;
    const float iR = 1.0f / aR, iB = 1.0f / aB;
    /* scratch[i] at an R or B site = its balanced value minus its HA G (the colour
     * difference); G sites are not written. Same-colour neighbours (x +- 2, y +- 2)
     * share the site's balance; the +- 1 neighbours are G, which is never balanced. */
    float *dif = scratch;

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < h; ++y)
    {
        const uint16_t *row = bayer + (size_t)y * (size_t)w;
        const uint16_t *u1 = bayer + (size_t)debayer_hacd_reflect(y - 1, h) * (size_t)w;
        const uint16_t *d1 = bayer + (size_t)debayer_hacd_reflect(y + 1, h) * (size_t)w;
        const uint16_t *u2 = bayer + (size_t)debayer_hacd_reflect(y - 2, h) * (size_t)w;
        const uint16_t *d2 = bayer + (size_t)debayer_hacd_reflect(y + 2, h) * (size_t)w;
        const float ck = (y & 1) ? cB : cR, ik = (y & 1) ? iB : iR;
        float *dd = dif + (size_t)y * (size_t)w;
        int x = (y & 1);
        /* The border sites mirror; the interior (x - 2 >= 0, x + 2 < w) indexes directly. */
        for (; x < 2; x += 2)
            dd[x] = debayer_hacd_site(row, u1, d1, u2, d2, x, debayer_hacd_reflect(x - 1, w), debayer_hacd_reflect(x + 1, w),
                                      debayer_hacd_reflect(x - 2, w), debayer_hacd_reflect(x + 2, w), ck, ik);
        for (; x + 2 < w; x += 2)
            dd[x] = debayer_hacd_site(row, u1, d1, u2, d2, x, x - 1, x + 1, x - 2, x + 2, ck, ik);
        for (; x < w; x += 2)
            dd[x] = debayer_hacd_site(row, u1, d1, u2, d2, x, debayer_hacd_reflect(x - 1, w), debayer_hacd_reflect(x + 1, w),
                                      debayer_hacd_reflect(x - 2, w), debayer_hacd_reflect(x + 2, w), ck, ik);
    }

    #pragma omp parallel for if(threads > 1) num_threads(threads)
    for (int y = 0; y < h; ++y)
    {
        const size_t ro = (size_t)y * (size_t)w;
        const size_t uo = (size_t)debayer_hacd_reflect(y - 1, h) * (size_t)w;
        const size_t dof = (size_t)debayer_hacd_reflect(y + 1, h) * (size_t)w;
        uint16_t *out = rgb + ro * 3u;
        const int ye = (y & 1) == 0;
        /* x = 0 and x = w - 1 mirror; the interior runs in (odd, even) pairs so each call's
         * column parity is a constant. */
        debayer_hacd_px(out, dif, ro, uo, dof, 0, 1, 1, ye, 1, aR, cR, aB, cB, keep);
        int x = 1;
        for (; x + 2 < w; x += 2)
        {
            debayer_hacd_px(out, dif, ro, uo, dof, x, x - 1, x + 1, ye, 0, aR, cR, aB, cB, keep);
            debayer_hacd_px(out, dif, ro, uo, dof, x + 1, x, x + 2, ye, 1, aR, cR, aB, cB, keep);
        }
        for (; x < w - 1; ++x)
            debayer_hacd_px(out, dif, ro, uo, dof, x, x - 1, x + 1, ye, (x & 1) == 0, aR, cR, aB, cB, keep);
        debayer_hacd_px(out, dif, ro, uo, dof, w - 1, w - 2, debayer_hacd_reflect(w, w), ye, ((w - 1) & 1) == 0, aR, cR,
                        aB, cB, keep);
        debayer_hacd_select_row(out, bayer, dif, w, h, y, keep, aR, aB);
        debayer_hacd_range_row(out, bayer, w, h, y, keep);
    }
    return 1;
}

int debayer_reduced_hacd_chroma_rgb16(uint16_t *rgb, const uint16_t *bayer, int w, int h, int threads,
                                      float *scratch)
{
    return debayer_hacd_impl(rgb, bayer, w, h, threads, scratch, NULL, NULL);
}

int debayer_reduced_hacd_chroma121_rgb16(uint16_t *filtered, uint16_t *rgb, const uint16_t *bayer, int w, int h,
                                         int threads, float *scratch)
{
    if (!filtered) return 0;
    float sel[2] = { 1.0f, 1.0f };
    int keep = 3;
    if (!debayer_hacd_impl(rgb, bayer, w, h, threads, scratch, sel, &keep)) return 0;
    debayer_reduced_hchroma121_impl(filtered, rgb, w, h, threads, 1, sel, keep);
    return 1;
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
            /* Odd-width right edge: as in debayerBasicU16's scalar body. */
            const int r2 = (x + 2 < width) ? 2 : 0;

            /* Bayer pixel indexes */
            int bPix[16] = {
                ( pixm1-1 ), ( pixm1 ), ( pixm1+1 ), ( pixm1+r2 ),
                ( pix - 1 ), (  pix  ), ( pix + 1 ), ( pix + r2 ),
                ( pixp1-1 ), ( pixp1 ), ( pixp1+1 ), ( pixp1+r2 ),
                ( pixp2-1 ), ( pixp2 ), ( pixp2+1 ), ( pixp2+r2 ),
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
