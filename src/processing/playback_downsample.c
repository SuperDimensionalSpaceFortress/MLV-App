/* Phase 4B: fused Bayer-to-RGB downsample-and-debayer.
 *
 * Overview: at the end of llrawproc the bayer image is full-resolution
 * uint16 RGGB. This module collapses that into a half- or quarter-res
 * RGB16 image via per-channel block averaging. It is the keystone of the
 * adaptive-playback-resolution feature — every downstream stage
 * (debayer, processing, the 8-bit reduce) then runs on a 1/4 or 1/16
 * pixel count, which converts the 5K HQ Dual ISO recon cadence from
 * ~120 ms/frame to a target of ~35 ms/frame.
 *
 * The per-2x2-block channel-averaging strategy is described in
 * .claude-state/profiling/20260425-phase4-scoping/PLAN.md Section 1
 * option (e). The G channel is averaged across the two green positions in
 * the RGGB tile; this is intrinsically anti-aliased and keeps the chroma
 * relationship between R/G/B locally consistent.
 *
 * Dispatch: pthread_once + AVX2 fast path on x86_64. Kill-switches:
 *   MLVAPP_DISABLE_AVX2              (global)
 *   MLVAPP_DISABLE_AVX2_DOWNSAMPLE   (per-kernel)
 *
 * Threading: outer rows are OMP-parallel for both scalar and AVX2
 * variants when threads > 1. Each row is independent, no false sharing.
 */

#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <pthread.h>

#include <omp.h>

#include "playback_downsample.h"

#if defined(__GNUC__) && !defined(__clang__) && (defined(__x86_64__) || defined(__i386__))
#include <immintrin.h>
#define PL_DOWNSAMPLE_AVX2_AVAILABLE 1
#endif

/* ---------------------------------------------------------------------- */
/* Scalar reference kernels — correctness baseline. */

static void pl_downsample_2x_row_scalar(const uint16_t * __restrict bayer_in,
                                        int in_w,
                                        uint16_t * __restrict rgb_out,
                                        int y_out,
                                        int bit_shift)
{
    const int out_w = in_w >> 1;
    const uint16_t * __restrict row0 = bayer_in + ((size_t)(y_out * 2) * (size_t)in_w);
    const uint16_t * __restrict row1 = row0 + in_w;

    /* RGGB tile:
     *   row0: R G R G ...
     *   row1: G B G B ...
     */
    for (int x_out = 0; x_out < out_w; ++x_out)
    {
        const int x_src = x_out * 2;
        const uint32_t r  = row0[x_src];
        const uint32_t g0 = row0[x_src + 1];
        const uint32_t g1 = row1[x_src];
        const uint32_t b  = row1[x_src + 1];
        const uint32_t g  = (g0 + g1) >> 1;

        rgb_out[3 * x_out + 0] = (uint16_t)(r << bit_shift);
        rgb_out[3 * x_out + 1] = (uint16_t)(g << bit_shift);
        rgb_out[3 * x_out + 2] = (uint16_t)(b << bit_shift);
    }
}

static void pl_downsample_4x_row_scalar(const uint16_t * __restrict bayer_in,
                                        int in_w,
                                        uint16_t * __restrict rgb_out,
                                        int y_out,
                                        int bit_shift)
{
    const int out_w = in_w >> 2;
    const uint16_t * __restrict row0 = bayer_in + ((size_t)(y_out * 4) * (size_t)in_w);
    const uint16_t * __restrict row1 = row0 + in_w;
    const uint16_t * __restrict row2 = row1 + in_w;
    const uint16_t * __restrict row3 = row2 + in_w;

    /* 4x4 RGGB tile:
     *   row0: R G R G   (R at cols 0,2; G at cols 1,3)
     *   row1: G B G B   (G at cols 0,2; B at cols 1,3)
     *   row2: R G R G
     *   row3: G B G B
     */
    for (int x_out = 0; x_out < out_w; ++x_out)
    {
        const int x_src = x_out * 4;

        /* 4 R taps: row0[0,2], row2[0,2] */
        const uint32_t r0 = row0[x_src + 0];
        const uint32_t r1 = row0[x_src + 2];
        const uint32_t r2 = row2[x_src + 0];
        const uint32_t r3 = row2[x_src + 2];
        const uint32_t r  = (r0 + r1 + r2 + r3) >> 2;

        /* 4 B taps: row1[1,3], row3[1,3] */
        const uint32_t b0 = row1[x_src + 1];
        const uint32_t b1 = row1[x_src + 3];
        const uint32_t b2 = row3[x_src + 1];
        const uint32_t b3 = row3[x_src + 3];
        const uint32_t b  = (b0 + b1 + b2 + b3) >> 2;

        /* 8 G taps:
         *   row0 cols 1,3 ; row1 cols 0,2 ; row2 cols 1,3 ; row3 cols 0,2 */
        const uint32_t g0 = row0[x_src + 1];
        const uint32_t g1 = row0[x_src + 3];
        const uint32_t g2 = row1[x_src + 0];
        const uint32_t g3 = row1[x_src + 2];
        const uint32_t g4 = row2[x_src + 1];
        const uint32_t g5 = row2[x_src + 3];
        const uint32_t g6 = row3[x_src + 0];
        const uint32_t g7 = row3[x_src + 2];
        const uint32_t g  = (g0 + g1 + g2 + g3 + g4 + g5 + g6 + g7) >> 3;

        rgb_out[3 * x_out + 0] = (uint16_t)(r << bit_shift);
        rgb_out[3 * x_out + 1] = (uint16_t)(g << bit_shift);
        rgb_out[3 * x_out + 2] = (uint16_t)(b << bit_shift);
    }
}

static void pl_downsample_8x_row_scalar(const uint16_t * __restrict bayer_in,
                                        int in_w,
                                        uint16_t * __restrict rgb_out,
                                        int y_out,
                                        int bit_shift)
{
    const int out_w = in_w >> 3;
    const uint16_t * __restrict rows[8];
    rows[0] = bayer_in + ((size_t)(y_out * 8) * (size_t)in_w);
    for (int y = 1; y < 8; ++y)
    {
        rows[y] = rows[y - 1] + in_w;
    }

    for (int x_out = 0; x_out < out_w; ++x_out)
    {
        const int x_src = x_out * 8;
        uint32_t r = 0;
        uint32_t g = 0;
        uint32_t b = 0;

        for (int y = 0; y < 8; ++y)
        {
            const uint16_t * __restrict row = rows[y] + x_src;
            if ((y & 1) == 0)
            {
                for (int x = 0; x < 8; x += 2)
                {
                    r += row[x];
                    g += row[x + 1];
                }
            }
            else
            {
                for (int x = 0; x < 8; x += 2)
                {
                    g += row[x];
                    b += row[x + 1];
                }
            }
        }

        rgb_out[3 * x_out + 0] = (uint16_t)((r >> 4) << bit_shift);
        rgb_out[3 * x_out + 1] = (uint16_t)((g >> 5) << bit_shift);
        rgb_out[3 * x_out + 2] = (uint16_t)((b >> 4) << bit_shift);
    }
}

/* ---------------------------------------------------------------------- */
/* AVX2 fast paths. */

#ifdef PL_DOWNSAMPLE_AVX2_AVAILABLE

/* 2x kernel.
 *
 * Approach: process 16 output pixels per SIMD iter (= 32 source columns
 * per row across two source rows). Use _mm256_load_si256 to grab 16
 * consecutive uint16 lanes from each source row, then split into the
 * even-indexed (R / G1) and odd-indexed (G2 / B) lanes. Average the two
 * G lanes per output pixel, then apply the bit_shift left-shift.
 *
 * AoS-3 store: GCC autovectorises stride-3 stores poorly when fed AVX2
 * registers, so we spill the three channel vectors to 32-byte-aligned
 * scratch and emit the interleaved triplets scalarly. The scalar
 * interleave runs hot in L1 because it reads the data we just stored.
 *
 * Edge handling: the tail (out_w not divisible by 16) falls through to
 * scalar.
 */
__attribute__((target("avx2")))
static void pl_downsample_2x_row_avx2(const uint16_t * __restrict bayer_in,
                                      int in_w,
                                      uint16_t * __restrict rgb_out,
                                      int y_out,
                                      int bit_shift)
{
    const int out_w = in_w >> 1;
    const uint16_t * __restrict row0 = bayer_in + ((size_t)(y_out * 2) * (size_t)in_w);
    const uint16_t * __restrict row1 = row0 + in_w;

    /* mask to extract even-indexed words from each lane half */
    const __m256i mask_evens = _mm256_set1_epi32(0x0000FFFF);
    const __m256i shift_count = _mm256_set_epi64x(0, bit_shift, 0, bit_shift);

    /* alignas(32) scratch */
    uint16_t r_buf[16] __attribute__((aligned(32)));
    uint16_t g_buf[16] __attribute__((aligned(32)));
    uint16_t b_buf[16] __attribute__((aligned(32)));

    int x_out = 0;
    /* Each iter consumes 32 source columns (in two rows) and produces
     * 16 output RGB triplets. */
    for (; x_out + 16 <= out_w; x_out += 16)
    {
        const int x_src = x_out * 2;
        /* Load 32 source columns from each row (32 * 2 bytes = 64 bytes
         * = two 256-bit vectors). */
        const __m256i row0_lo = _mm256_loadu_si256((const __m256i *)(row0 + x_src + 0));
        const __m256i row0_hi = _mm256_loadu_si256((const __m256i *)(row0 + x_src + 16));
        const __m256i row1_lo = _mm256_loadu_si256((const __m256i *)(row1 + x_src + 0));
        const __m256i row1_hi = _mm256_loadu_si256((const __m256i *)(row1 + x_src + 16));

        /* Extract even-indexed (= R from row0 / G2 from row1) lanes:
         *   even lanes = src & 0x0000FFFF, then pack 2 vectors of 8 evens
         *   into 1 vector of 16 evens.
         * Use _mm256_packus_epi32 which packs adjacent 32-bit lanes into
         * 16-bit. Need to permute lanes back because packus interleaves
         * the two halves of each 256-bit register. */
        const __m256i row0_evens_lo = _mm256_and_si256(row0_lo, mask_evens);
        const __m256i row0_evens_hi = _mm256_and_si256(row0_hi, mask_evens);
        const __m256i row1_evens_lo = _mm256_and_si256(row1_lo, mask_evens);
        const __m256i row1_evens_hi = _mm256_and_si256(row1_hi, mask_evens);

        /* odd-indexed: shift right by 16 and mask. */
        const __m256i row0_odds_lo = _mm256_srli_epi32(row0_lo, 16);
        const __m256i row0_odds_hi = _mm256_srli_epi32(row0_hi, 16);
        const __m256i row1_odds_lo = _mm256_srli_epi32(row1_lo, 16);
        const __m256i row1_odds_hi = _mm256_srli_epi32(row1_hi, 16);

        /* Pack pairs into 16-lane uint16 vectors. After packus the lane
         * order is: [lo0..lo7, hi0..hi7, lo8..lo15, hi8..hi15] across the
         * 256-bit register because AVX packus operates per 128-bit lane.
         * We want [evens_lo, evens_hi] → straight uint16 vector of the 16
         * R values. Use packus then permute4x64 with imm 0xD8 = 11 01 10 00
         * which moves lane1<->lane2 to fix interleave. */
        const __m256i r_packed = _mm256_permute4x64_epi64(
            _mm256_packus_epi32(row0_evens_lo, row0_evens_hi),
            0xD8);
        const __m256i g1_packed = _mm256_permute4x64_epi64(
            _mm256_packus_epi32(row0_odds_lo, row0_odds_hi),
            0xD8);
        const __m256i g2_packed = _mm256_permute4x64_epi64(
            _mm256_packus_epi32(row1_evens_lo, row1_evens_hi),
            0xD8);
        const __m256i b_packed = _mm256_permute4x64_epi64(
            _mm256_packus_epi32(row1_odds_lo, row1_odds_hi),
            0xD8);

        /* G = (g1 + g2) >> 1 (truncating), matching scalar (g0+g1)>>1.
         * _mm256_avg_epu16 rounds half-up; we use the avg-of-avg trick:
         *   (a+b)>>1 (truncate) == avg(a,b) - ((a^b)&1) */
        const __m256i g_xor = _mm256_xor_si256(g1_packed, g2_packed);
        const __m256i g_avg = _mm256_avg_epu16(g1_packed, g2_packed);
        const __m256i g_lsb = _mm256_and_si256(g_xor, _mm256_set1_epi16(1));
        const __m256i g_packed = _mm256_sub_epi16(g_avg, g_lsb);

        /* Apply bit_shift left-shift. Use _mm256_sll_epi16 with a
         * 64-bit count vector. */
        const __m256i r_shifted = _mm256_sll_epi16(r_packed, _mm256_castsi256_si128(shift_count));
        const __m256i g_shifted = _mm256_sll_epi16(g_packed, _mm256_castsi256_si128(shift_count));
        const __m256i b_shifted = _mm256_sll_epi16(b_packed, _mm256_castsi256_si128(shift_count));

        _mm256_store_si256((__m256i *)r_buf, r_shifted);
        _mm256_store_si256((__m256i *)g_buf, g_shifted);
        _mm256_store_si256((__m256i *)b_buf, b_shifted);

        /* AoS-3 interleave (16 RGB triplets = 48 uint16s). */
        uint16_t * __restrict dst = rgb_out + (size_t)x_out * 3u;
        for (int k = 0; k < 16; ++k)
        {
            dst[3 * k + 0] = r_buf[k];
            dst[3 * k + 1] = g_buf[k];
            dst[3 * k + 2] = b_buf[k];
        }
    }

    /* Tail: run the scalar kernel on the remaining columns. */
    if (x_out < out_w)
    {
        const uint16_t * __restrict r0 = row0 + (x_out * 2);
        const uint16_t * __restrict r1 = row1 + (x_out * 2);
        uint16_t * __restrict dst = rgb_out + (size_t)x_out * 3u;
        for (int xo = x_out; xo < out_w; ++xo)
        {
            const uint32_t r  = r0[0];
            const uint32_t g0 = r0[1];
            const uint32_t g1 = r1[0];
            const uint32_t b  = r1[1];
            const uint32_t g  = (g0 + g1) >> 1;
            dst[0] = (uint16_t)(r << bit_shift);
            dst[1] = (uint16_t)(g << bit_shift);
            dst[2] = (uint16_t)(b << bit_shift);
            r0 += 2;
            r1 += 2;
            dst += 3;
        }
    }
}

/* 4x kernel.
 *
 * Process 8 output pixels per SIMD iter (= 32 source columns spanning
 * 4 source rows). Using 256-bit vectors of 8 uint32 lanes lets us hold
 * the running sum without overflow (each tap is at most 0xFFFF, and we
 * sum at most 8 taps -> 0x7FFF8 fits easily in 32-bit).
 *
 * Strategy: build 8 x uint32 vectors of taps, sum, divide by 4 (R/B) or 8
 * (G), then pack down to uint16 and apply bit_shift.
 */
__attribute__((target("avx2")))
static void pl_downsample_4x_row_avx2(const uint16_t * __restrict bayer_in,
                                      int in_w,
                                      uint16_t * __restrict rgb_out,
                                      int y_out,
                                      int bit_shift)
{
    const int out_w = in_w >> 2;
    const uint16_t * __restrict row0 = bayer_in + ((size_t)(y_out * 4) * (size_t)in_w);
    const uint16_t * __restrict row1 = row0 + in_w;
    const uint16_t * __restrict row2 = row1 + in_w;
    const uint16_t * __restrict row3 = row2 + in_w;

    const __m256i shift_count = _mm256_set_epi64x(0, bit_shift, 0, bit_shift);

    /* alignas(32) scratch */
    uint16_t r_buf[16] __attribute__((aligned(32)));
    uint16_t g_buf[16] __attribute__((aligned(32)));
    uint16_t b_buf[16] __attribute__((aligned(32)));

    int x_out = 0;
    /* Each iter consumes 32 source columns and produces 8 output RGB
     * triplets. */
    for (; x_out + 8 <= out_w; x_out += 8)
    {
        const int x_src = x_out * 4;

        /* Load 16 consecutive uint16 lanes from each source row.
         * That covers source x in [x_src, x_src+16). For 8 output pixels
         * we span source [x_src, x_src+32) — so we need TWO loads per row.
         * Instead, we'll process 8 output pixels but use lanes only over
         * 32 source columns. Let's split into two halves of 4 outputs each
         * for clarity, each processed as a single vector iter. */

        /* For 4 output pixels (16 source columns), one __m256i load of 16
         * uint16 from each row is enough. So unroll: process 4 outputs,
         * then 4 more, both in this SIMD iter. */

        /* Load 16 source cols from each row into uint16x16 vectors. */
        const __m256i s0_lo = _mm256_loadu_si256((const __m256i *)(row0 + x_src + 0));
        const __m256i s1_lo = _mm256_loadu_si256((const __m256i *)(row1 + x_src + 0));
        const __m256i s2_lo = _mm256_loadu_si256((const __m256i *)(row2 + x_src + 0));
        const __m256i s3_lo = _mm256_loadu_si256((const __m256i *)(row3 + x_src + 0));

        const __m256i s0_hi = _mm256_loadu_si256((const __m256i *)(row0 + x_src + 16));
        const __m256i s1_hi = _mm256_loadu_si256((const __m256i *)(row1 + x_src + 16));
        const __m256i s2_hi = _mm256_loadu_si256((const __m256i *)(row2 + x_src + 16));
        const __m256i s3_hi = _mm256_loadu_si256((const __m256i *)(row3 + x_src + 16));

        /* For the *_lo vectors (16 uint16 lanes covering source cols 0..15),
         * the output pixels are k=0..3, with x_src+0..3 covering output 0,
         * x_src+4..7 covering output 1, etc. So output[k] needs taps at
         * source columns (x_src + 4k + {0,1,2,3}).
         *
         * We can fall back to scalar reduction here since the 4x kernel
         * is dominated by the load and the per-output-pixel arithmetic
         * is many fewer ops than the 2x case. The hot work is the loads. */

        /* Spill the 16-col window to scratch and run a tight scalar
         * inner loop. With s*_lo / s*_hi resident in registers and the
         * scalar reads going to the L1, this is fast enough. */
        uint16_t s0_w[32] __attribute__((aligned(32)));
        uint16_t s1_w[32] __attribute__((aligned(32)));
        uint16_t s2_w[32] __attribute__((aligned(32)));
        uint16_t s3_w[32] __attribute__((aligned(32)));
        _mm256_store_si256((__m256i *)(s0_w + 0), s0_lo);
        _mm256_store_si256((__m256i *)(s0_w + 16), s0_hi);
        _mm256_store_si256((__m256i *)(s1_w + 0), s1_lo);
        _mm256_store_si256((__m256i *)(s1_w + 16), s1_hi);
        _mm256_store_si256((__m256i *)(s2_w + 0), s2_lo);
        _mm256_store_si256((__m256i *)(s2_w + 16), s2_hi);
        _mm256_store_si256((__m256i *)(s3_w + 0), s3_lo);
        _mm256_store_si256((__m256i *)(s3_w + 16), s3_hi);

        for (int k = 0; k < 8; ++k)
        {
            const int xs = k * 4;
            const uint32_t r =
                ((uint32_t)s0_w[xs + 0] + (uint32_t)s0_w[xs + 2]
                + (uint32_t)s2_w[xs + 0] + (uint32_t)s2_w[xs + 2]) >> 2;
            const uint32_t b =
                ((uint32_t)s1_w[xs + 1] + (uint32_t)s1_w[xs + 3]
                + (uint32_t)s3_w[xs + 1] + (uint32_t)s3_w[xs + 3]) >> 2;
            const uint32_t g =
                ((uint32_t)s0_w[xs + 1] + (uint32_t)s0_w[xs + 3]
                + (uint32_t)s1_w[xs + 0] + (uint32_t)s1_w[xs + 2]
                + (uint32_t)s2_w[xs + 1] + (uint32_t)s2_w[xs + 3]
                + (uint32_t)s3_w[xs + 0] + (uint32_t)s3_w[xs + 2]) >> 3;

            r_buf[k] = (uint16_t)r;
            g_buf[k] = (uint16_t)g;
            b_buf[k] = (uint16_t)b;
        }

        /* Apply bit_shift left-shift via vector op for SIMD ILP. */
        const __m128i r_v = _mm_load_si128((const __m128i *)r_buf);
        const __m128i g_v = _mm_load_si128((const __m128i *)g_buf);
        const __m128i b_v = _mm_load_si128((const __m128i *)b_buf);
        const __m128i r_s = _mm_sll_epi16(r_v, _mm256_castsi256_si128(shift_count));
        const __m128i g_s = _mm_sll_epi16(g_v, _mm256_castsi256_si128(shift_count));
        const __m128i b_s = _mm_sll_epi16(b_v, _mm256_castsi256_si128(shift_count));
        _mm_store_si128((__m128i *)r_buf, r_s);
        _mm_store_si128((__m128i *)g_buf, g_s);
        _mm_store_si128((__m128i *)b_buf, b_s);

        uint16_t * __restrict dst = rgb_out + (size_t)x_out * 3u;
        for (int k = 0; k < 8; ++k)
        {
            dst[3 * k + 0] = r_buf[k];
            dst[3 * k + 1] = g_buf[k];
            dst[3 * k + 2] = b_buf[k];
        }
    }

    /* Tail. */
    if (x_out < out_w)
    {
        for (int xo = x_out; xo < out_w; ++xo)
        {
            const int x_src = xo * 4;
            const uint32_t r0 = row0[x_src + 0];
            const uint32_t r1 = row0[x_src + 2];
            const uint32_t r2 = row2[x_src + 0];
            const uint32_t r3 = row2[x_src + 2];
            const uint32_t r  = (r0 + r1 + r2 + r3) >> 2;

            const uint32_t b0 = row1[x_src + 1];
            const uint32_t b1 = row1[x_src + 3];
            const uint32_t b2 = row3[x_src + 1];
            const uint32_t b3 = row3[x_src + 3];
            const uint32_t b  = (b0 + b1 + b2 + b3) >> 2;

            const uint32_t g  = ((uint32_t)row0[x_src + 1] + (uint32_t)row0[x_src + 3]
                              + (uint32_t)row1[x_src + 0] + (uint32_t)row1[x_src + 2]
                              + (uint32_t)row2[x_src + 1] + (uint32_t)row2[x_src + 3]
                              + (uint32_t)row3[x_src + 0] + (uint32_t)row3[x_src + 2]) >> 3;

            uint16_t * __restrict dst = rgb_out + (size_t)xo * 3u;
            dst[0] = (uint16_t)(r << bit_shift);
            dst[1] = (uint16_t)(g << bit_shift);
            dst[2] = (uint16_t)(b << bit_shift);
        }
    }
}

#endif /* PL_DOWNSAMPLE_AVX2_AVAILABLE */

/* ---------------------------------------------------------------------- */
/* Dispatch. */

typedef void (*pl_downsample_row_fn_t)(const uint16_t *,
                                       int,
                                       uint16_t *,
                                       int,
                                       int);

static int pl_downsample_env_truthy(const char * v)
{
    if (!v || !*v) return 0;
    if (!strcmp(v, "1") || !strcmp(v, "true") || !strcmp(v, "TRUE")
     || !strcmp(v, "True") || !strcmp(v, "yes") || !strcmp(v, "on")) return 1;
    return 0;
}

static pthread_once_t g_pl_downsample_dispatch_once = PTHREAD_ONCE_INIT;
static int g_pl_downsample_use_avx2 = 0;
static pl_downsample_row_fn_t g_pl_downsample_2x_row_fn = NULL;
static pl_downsample_row_fn_t g_pl_downsample_4x_row_fn = NULL;

static void pl_downsample_dispatch_init(void)
{
    int use_avx2 = 0;
#ifdef PL_DOWNSAMPLE_AVX2_AVAILABLE
    __builtin_cpu_init();
    use_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
    if (pl_downsample_env_truthy(getenv("MLVAPP_DISABLE_AVX2"))) use_avx2 = 0;
    if (pl_downsample_env_truthy(getenv("MLVAPP_DISABLE_AVX2_DOWNSAMPLE"))) use_avx2 = 0;

    g_pl_downsample_use_avx2 = use_avx2;
#ifdef PL_DOWNSAMPLE_AVX2_AVAILABLE
    g_pl_downsample_2x_row_fn = use_avx2 ? pl_downsample_2x_row_avx2
                                         : pl_downsample_2x_row_scalar;
    g_pl_downsample_4x_row_fn = use_avx2 ? pl_downsample_4x_row_avx2
                                         : pl_downsample_4x_row_scalar;
#else
    g_pl_downsample_2x_row_fn = pl_downsample_2x_row_scalar;
    g_pl_downsample_4x_row_fn = pl_downsample_4x_row_scalar;
#endif
}

int plDownsampleAvx2Active(void)
{
    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);
    return g_pl_downsample_use_avx2;
}

int plDownsampleReinitDispatchForTesting(void)
{
    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);
    pl_downsample_dispatch_init();
    return g_pl_downsample_use_avx2;
}

/* ---------------------------------------------------------------------- */
/* Public entry points. */

void pl_downsample_bayer_to_rgb_2x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads)
{
    if (!bayer_in || !rgb_out || in_w < 2 || in_h < 2) return;
    if (in_w & 1) return; /* must be even */
    if (in_h & 1) return;

    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);

    const int out_h = in_h >> 1;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 1) * 3u);
            g_pl_downsample_2x_row_fn(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 1) * 3u);
            g_pl_downsample_2x_row_fn(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
}

void pl_downsample_bayer_to_rgb_4x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads)
{
    if (!bayer_in || !rgb_out || in_w < 4 || in_h < 4) return;
    if (in_w & 3) return; /* must be multiple of 4 */
    if (in_h & 3) return;

    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);

    const int out_h = in_h >> 2;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 2) * 3u);
            g_pl_downsample_4x_row_fn(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 2) * 3u);
            g_pl_downsample_4x_row_fn(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
}

void pl_downsample_bayer_to_rgb_8x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads)
{
    if (!bayer_in || !rgb_out || in_w < 8 || in_h < 8) return;

    const int out_h = in_h >> 3;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 3) * 3u);
            pl_downsample_8x_row_scalar(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            uint16_t * __restrict dst_row = rgb_out + ((size_t)y_out * (size_t)(in_w >> 3) * 3u);
            pl_downsample_8x_row_scalar(bayer_in, in_w, dst_row, y_out, bit_shift);
        }
    }
}

/* ---------------------------------------------------------------------- */
/* Phase 4B-v2: Bayer-preserving downsample kernels for the
 * downsample-BEFORE-llrawproc path. The output is still uint16 RGGB Bayer
 * (not RGB), so HQ Dual ISO recon and the rest of llrawproc can run on the
 * smaller buffer.
 *
 * The 4-row dual-ISO bright/dark pattern is preserved by sampling complete
 * 4-row source blocks and skipping forward by an integer multiple of 4
 * rows for the next output block. Output row indices satisfy
 *   src_row[y_out] % 4 == y_out % 4
 * so that BRIGHT_ROW (= is_bright[y%4]) at the recon step yields the same
 * classification as on the full-res input.
 *
 * X axis: 2-tap same-Bayer-position averaging (in-row), preserves RGGB.
 *
 * Scale=4 mode (block-stride 16): out_dim = (in_w/4, in_h/4).
 *   Requires in_w % 4 == 0 and in_h % 16 == 0.
 *   src_row = (y_out / 4) * 16 + (y_out % 4).
 * Scale=2 mode (block-stride 8): out_dim = (in_w/2, in_h/2).
 *   Requires in_w % 2 == 0 and in_h % 8 == 0.
 *   src_row = (y_out / 4) * 8 + (y_out % 4).
 *
 * Both kernels use 1x2 same-Bayer-position averaging in X (2 source taps
 * per output cell). They run scalar today; AVX2 fast paths can be added
 * later if profiling shows the kernel is hot. Even at 5K resolution the
 * scale=4 bayer-to-bayer pass writes only 1302*768 = 1M uint16 per frame —
 * memory-bandwidth bound, not compute bound.
 */

static int pl_downsample_bayer_to_bayer_4x_scalar(const uint16_t * __restrict bayer_in,
                                                  int in_w,
                                                  int in_h,
                                                  uint16_t * __restrict bayer_out,
                                                  int * out_w_p,
                                                  int * out_h_p,
                                                  int threads)
{
    if (!bayer_in || !bayer_out) return 1;
    if (in_w < 4 || in_h < 16) return 1;
    if (in_w & 3) return 1; /* multiple of 4 */
    if (in_h & 15) return 1; /* multiple of 16 (block stride) */

    const int out_w = in_w >> 2;
    const int out_h = in_h >> 2;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    /* Each output row processes one src row. Loop is parallel-safe over y_out
     * because src_row mapping is bijective on kept rows. */
    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 16 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;

            /* even x_out (R or G2): src cols x*4 + 0, x*4 + 2.
             * odd x_out (G1 or B): src cols x*4 + 1, x*4 + 3.
             * We unroll the inner loop in pairs so two consecutive
             * x_out values cover all 4 source cols of one tile. */
            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
            /* Tail: out_w even (always, since in_w % 4 == 0 → out_w even),
             * so no tail handling needed. */
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 16 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;

            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
        }
    }
    return 0;
}

static int pl_downsample_bayer_to_bayer_2x_scalar(const uint16_t * __restrict bayer_in,
                                                  int in_w,
                                                  int in_h,
                                                  uint16_t * __restrict bayer_out,
                                                  int * out_w_p,
                                                  int * out_h_p,
                                                  int threads)
{
    if (!bayer_in || !bayer_out) return 1;
    if (in_w < 4 || in_h < 8) return 1;
    if (in_w & 3) return 1;
    if (in_h & 7) return 1; /* multiple of 8 (block stride) */

    const int out_w = in_w >> 1;
    const int out_h = in_h >> 1;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    /* X downsample by 2 while preserving the RGGB pattern:
     * output cells are processed in Bayer pairs. For each 4-column source
     * tile, the even output cell averages source cols 0 and 2 (same Bayer
     * color), while the odd output cell averages cols 1 and 3. */
    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 8 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 2;
                const uint32_t even0 = srow[xs + 0];
                const uint32_t even1 = srow[xs + 2];
                const uint32_t odd0 = srow[xs + 1];
                const uint32_t odd1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((even0 + even1) >> 1);
                drow[x_out + 1] = (uint16_t)((odd0 + odd1) >> 1);
            }
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 8 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 2;
                const uint32_t even0 = srow[xs + 0];
                const uint32_t even1 = srow[xs + 2];
                const uint32_t odd0 = srow[xs + 1];
                const uint32_t odd1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((even0 + even1) >> 1);
                drow[x_out + 1] = (uint16_t)((odd0 + odd1) >> 1);
            }
        }
    }
    return 0;
}

static int pl_downsample_bayer_to_bayer_8x_scalar(const uint16_t * __restrict bayer_in,
                                                  int in_w,
                                                  int in_h,
                                                  uint16_t * __restrict bayer_out,
                                                  int * out_w_p,
                                                  int * out_h_p,
                                                  int threads)
{
    if (!bayer_in || !bayer_out) return 1;
    if (in_w < 16 || in_h < 32) return 1;
    if (in_w & 15) return 1;  /* output width must remain Bayer-even */
    if (in_h & 31) return 1;  /* block stride 32 preserves 4-row ISO phase */

    const int out_w = in_w >> 3;
    const int out_h = in_h >> 3;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 32 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;

            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 8;
                const uint32_t a0 = srow[xs + 0];
                const uint32_t a1 = srow[xs + 2];
                const uint32_t a2 = srow[xs + 4];
                const uint32_t a3 = srow[xs + 6];
                const uint32_t b0 = srow[xs + 1];
                const uint32_t b1 = srow[xs + 3];
                const uint32_t b2 = srow[xs + 5];
                const uint32_t b3 = srow[xs + 7];
                drow[x_out + 0] = (uint16_t)((a0 + a1 + a2 + a3) >> 2);
                drow[x_out + 1] = (uint16_t)((b0 + b1 + b2 + b3) >> 2);
            }
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 32 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;

            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 8;
                const uint32_t a0 = srow[xs + 0];
                const uint32_t a1 = srow[xs + 2];
                const uint32_t a2 = srow[xs + 4];
                const uint32_t a3 = srow[xs + 6];
                const uint32_t b0 = srow[xs + 1];
                const uint32_t b1 = srow[xs + 3];
                const uint32_t b2 = srow[xs + 5];
                const uint32_t b3 = srow[xs + 7];
                drow[x_out + 0] = (uint16_t)((a0 + a1 + a2 + a3) >> 2);
                drow[x_out + 1] = (uint16_t)((b0 + b1 + b2 + b3) >> 2);
            }
        }
    }
    return 0;
}

#ifdef PL_DOWNSAMPLE_AVX2_AVAILABLE
__attribute__((target("avx2")))
static int pl_downsample_bayer_to_bayer_4x_avx2(const uint16_t * __restrict bayer_in,
                                                int in_w,
                                                int in_h,
                                                uint16_t * __restrict bayer_out,
                                                int * out_w_p,
                                                int * out_h_p,
                                                int threads)
{
    if (!bayer_in || !bayer_out) return 1;
    if (in_w < 4 || in_h < 16) return 1;
    if (in_w & 3) return 1;
    if (in_h & 15) return 1;

    const int out_w = in_w >> 2;
    const int out_h = in_h >> 2;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    /* AVX2 row processor.
     *
     * Per output row, we read 32 source uint16 lanes (= 8 output cells) per
     * 256-bit load. Source layout in lanes: [a0 a1 a2 a3 a4 a5 a6 a7 a8...]
     * where a_i is source col i.
     *
     * For each pair of output cells (R, G) we average:
     *   out[x]   = (a[xs+0] + a[xs+2]) >> 1   (R or G2)
     *   out[x+1] = (a[xs+1] + a[xs+3]) >> 1   (G1 or B)
     *
     * Strategy: take the 32-lane vector as four 8-lane groups. Within each
     * group of 4 source lanes we want to produce 2 output lanes:
     *   pair0: (lane0 + lane2) >> 1
     *   pair1: (lane1 + lane3) >> 1
     *
     * Implementation: shuffle to extract evens and odds into separate
     * vectors, then average pairs. We use _mm256_shufflelo_epi16 +
     * _mm256_shufflehi_epi16 to swap (lane0,lane1,lane2,lane3) →
     * (lane0,lane2,lane1,lane3). Then _mm256_unpacklo_epi32 /
     * _mm256_unpackhi_epi32 to gather even-indexed (lane0,lane2 from each
     * 4-group) and odd-indexed (lane1,lane3) into separate vectors.
     *
     * Simpler approach: scalar fallback for the inner loop, but vectorise
     * the load. The kernel is memory-bound at this scale (5K -> 1.25K
     * cols), so the load is what costs. We use a 32-lane scratch and a
     * tight scalar loop over it. */

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 16 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;

            int x_out = 0;
            /* Process 8 output cells (= 4 RGGB pairs = 32 source cols) per
             * AVX2 iter. Each output pair (R, G) consumes 4 source cols at
             * stride 8 (a 4-col tile of one bright/dark row, then skip
             * 4 cols to the next tile). 4 pairs * 8 cols = 32 src cols.
             *
             * Pre-2026-04-25 bug: the inner loop's `s = k * 4` walked
             * scratch in 4-col chunks expecting adjacency, but the scalar
             * walks src with stride 8 per pair. Outputs were only correct
             * for k=0 (first pair); pairs k=1..3 were sourced from the
             * wrong src cols (4..7 instead of 8..11, etc.). The original
             * test fixture was X-uniform so the wrong-source computation
             * happened to produce the same value as the correct one. The
             * Phase 4B-v3 Y-cropped 5K dual-iso path triggers the bug
             * visibly because the kernel runs on real (non-uniform) data.
             *
             * Fix: walk scratch with `s = k * 8` (stride 8 = 4-col tile of
             * one src row + the same tile of the next src row, but the
             * kernel only operates on a single row at a time so it's just
             * 8 src cols stride). 4 pairs * 8 src cols = 32 src cols loaded.
             *
             * Inner loop 2 was REMOVED — it was operating on the next
             * 16 src cols expecting a different output range, which is
             * fundamentally incompatible with the stride-8 source layout. */
            for (; x_out + 8 <= out_w; x_out += 8)
            {
                const int xs = x_out * 4;
                const __m256i v_lo = _mm256_loadu_si256((const __m256i *)(srow + xs + 0));
                const __m256i v_hi = _mm256_loadu_si256((const __m256i *)(srow + xs + 16));

                uint16_t scratch[32] __attribute__((aligned(32)));
                _mm256_store_si256((__m256i *)(scratch + 0), v_lo);
                _mm256_store_si256((__m256i *)(scratch + 16), v_hi);

                for (int k = 0; k < 4; ++k)
                {
                    const int s = k * 8;
                    const uint32_t r0 = scratch[s + 0];
                    const uint32_t r1 = scratch[s + 2];
                    const uint32_t g0 = scratch[s + 1];
                    const uint32_t g1 = scratch[s + 3];
                    drow[x_out + 2 * k + 0] = (uint16_t)((r0 + r1) >> 1);
                    drow[x_out + 2 * k + 1] = (uint16_t)((g0 + g1) >> 1);
                }
            }
            /* Tail: process 2 cells per iter using scalar. */
            for (; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int src_row_idx = (y_out >> 2) * 16 + (y_out & 3);
            const uint16_t * __restrict srow = bayer_in + (size_t)src_row_idx * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
            int x_out = 0;
            for (; x_out + 8 <= out_w; x_out += 8)
            {
                const int xs = x_out * 4;
                const __m256i v_lo = _mm256_loadu_si256((const __m256i *)(srow + xs + 0));
                const __m256i v_hi = _mm256_loadu_si256((const __m256i *)(srow + xs + 16));
                uint16_t scratch[32] __attribute__((aligned(32)));
                _mm256_store_si256((__m256i *)(scratch + 0), v_lo);
                _mm256_store_si256((__m256i *)(scratch + 16), v_hi);
                for (int k = 0; k < 4; ++k)
                {
                    const int s = k * 8;
                    const uint32_t r0 = scratch[s + 0];
                    const uint32_t r1 = scratch[s + 2];
                    const uint32_t g0 = scratch[s + 1];
                    const uint32_t g1 = scratch[s + 3];
                    drow[x_out + 2 * k + 0] = (uint16_t)((r0 + r1) >> 1);
                    drow[x_out + 2 * k + 1] = (uint16_t)((g0 + g1) >> 1);
                }
            }
            for (; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
        }
    }
    return 0;
}
#endif /* PL_DOWNSAMPLE_AVX2_AVAILABLE */

int pl_downsample_bayer_to_bayer_4x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads)
{
    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);
#ifdef PL_DOWNSAMPLE_AVX2_AVAILABLE
    if (g_pl_downsample_use_avx2)
    {
        return pl_downsample_bayer_to_bayer_4x_avx2(bayer_in, in_w, in_h,
                                                    bayer_out, out_w, out_h, threads);
    }
#endif
    return pl_downsample_bayer_to_bayer_4x_scalar(bayer_in, in_w, in_h,
                                                  bayer_out, out_w, out_h, threads);
}

int pl_downsample_bayer_to_bayer_2x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads)
{
    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);
    return pl_downsample_bayer_to_bayer_2x_scalar(bayer_in, in_w, in_h,
                                                  bayer_out, out_w, out_h, threads);
}

int pl_downsample_bayer_to_bayer_8x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads)
{
    pthread_once(&g_pl_downsample_dispatch_once, pl_downsample_dispatch_init);
    return pl_downsample_bayer_to_bayer_8x_scalar(bayer_in, in_w, in_h,
                                                  bayer_out, out_w, out_h, threads);
}

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r4: phase-correct tent bin (see the header).
 * Coordinates are doubled so every target and distance is an integer:
 * 2t = 2*S*o + S - 1, and a tap at s has weight radius2 - |2s - 2t| for
 * |2s - 2t| < radius2 (radius2 = 2S in columns, 4S in rows). */
static void pl_phase_tent_clamp_class(int * s, int modulus, int limit)
{
    if (*s < 0)
    {
        *s = ((*s % modulus) + modulus) % modulus;
    }
    else if (*s >= limit)
    {
        *s = (limit - 1) - ((((limit - 1) - *s) % modulus) + modulus) % modulus;
    }
}

static int pl_phase_tent_taps(int out_index, int scale, int modulus, int radius2,
                              int limit, int * src, uint32_t * wt)
{
    const int target2 = 2 * scale * out_index + scale - 1;
    const int cls = out_index % modulus;
    int n = 0;
    for (int s = (target2 - radius2) / 2 - modulus; s <= (target2 + radius2) / 2 + modulus; ++s)
    {
        if ((((s % modulus) + modulus) % modulus) != cls) continue;
        const int d2 = 2 * s - target2;
        const int ad2 = d2 < 0 ? -d2 : d2;
        if (ad2 >= radius2) continue;
        int clamped = s;
        pl_phase_tent_clamp_class(&clamped, modulus, limit);
        src[n] = clamped;
        wt[n] = (uint32_t)(radius2 - ad2);
        ++n;
    }
    return n;
}

static void pl_phase_tent_row(const uint16_t * __restrict bayer_in,
                              int in_w,
                              int in_h,
                              uint16_t * __restrict bayer_out,
                              int out_w,
                              int y_out,
                              int scale,
                              const int * col_src,
                              const uint32_t * col_wt,
                              int col_taps,
                              uint32_t den,
                              uint32_t * __restrict vrow)
{
    int row_src[4];
    uint32_t row_wt[4];
    const int row_taps = pl_phase_tent_taps(y_out, scale, 4, 4 * scale, in_h, row_src, row_wt);

    memset(vrow, 0, (size_t)in_w * sizeof(uint32_t));
    for (int t = 0; t < row_taps; ++t)
    {
        const uint16_t * __restrict srow = bayer_in + (size_t)row_src[t] * (size_t)in_w;
        const uint32_t w = row_wt[t];
        for (int x = 0; x < in_w; ++x) vrow[x] += w * (uint32_t)srow[x];
    }

    uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
    for (int x_out = 0; x_out < out_w; ++x_out)
    {
        const int * s = col_src + (size_t)x_out * 4u;
        const uint32_t * w = col_wt + (size_t)x_out * 4u;
        uint32_t acc = 0;
        for (int t = 0; t < col_taps; ++t) acc += w[t] * vrow[s[t]];
        drow[x_out] = (uint16_t)((acc + den / 2u) / den);
    }
}

int pl_downsample_bayer_to_bayer_phase_tent(const uint16_t * in,
                                            int in_w,
                                            int in_h,
                                            uint16_t * out,
                                            int scale,
                                            int * out_w_p,
                                            int * out_h_p,
                                            int threads)
{
    if (!in || !out) return 1;
    if (scale == 4)
    {
        if (in_w < 4 || in_h < 16 || (in_w & 3) || (in_h & 15)) return 1;
    }
    else if (scale == 2)
    {
        if (in_w < 4 || in_h < 8 || (in_w & 3) || (in_h & 7)) return 1;
    }
    else
    {
        return 1;
    }

    const int out_w = in_w / scale;
    const int out_h = in_h / scale;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    /* Column tap weights sum to S*S and row tap weights to 2*S*S: 512 at x4, 32 at x2. */
    const uint32_t den = (uint32_t)(scale * scale) * (uint32_t)(2 * scale * scale);
    int * col_src = (int *)malloc((size_t)out_w * 4u * sizeof(int));
    uint32_t * col_wt = (uint32_t *)malloc((size_t)out_w * 4u * sizeof(uint32_t));
    if (!col_src || !col_wt)
    {
        free(col_src);
        free(col_wt);
        return 1;
    }
    int col_taps = 0;
    for (int x_out = 0; x_out < out_w; ++x_out)
    {
        col_taps = pl_phase_tent_taps(x_out, scale, 2, 2 * scale, in_w,
                                      col_src + (size_t)x_out * 4u,
                                      col_wt + (size_t)x_out * 4u);
    }

    int failed = 0;
    if (threads > 1)
    {
        #pragma omp parallel num_threads(threads)
        {
            uint32_t * vrow = (uint32_t *)malloc((size_t)in_w * sizeof(uint32_t));
            if (!vrow)
            {
                #pragma omp atomic write
                failed = 1;
            }
            #pragma omp for
            for (int y_out = 0; y_out < out_h; ++y_out)
            {
                if (!vrow) continue;
                pl_phase_tent_row(in, in_w, in_h, out, out_w, y_out, scale,
                                  col_src, col_wt, col_taps, den, vrow);
            }
            free(vrow);
        }
    }
    else
    {
        uint32_t * vrow = (uint32_t *)malloc((size_t)in_w * sizeof(uint32_t));
        if (!vrow)
        {
            failed = 1;
        }
        else
        {
            for (int y_out = 0; y_out < out_h; ++y_out)
            {
                pl_phase_tent_row(in, in_w, in_h, out, out_w, y_out, scale,
                                  col_src, col_wt, col_taps, den, vrow);
            }
            free(vrow);
        }
    }
    free(col_src);
    free(col_wt);
    return failed ? 1 : 0;
}

/* X-only bayer-to-bayer 4x: same per-row 2-tap same-Bayer-position
 * average as the full kernel, but iterates over ALL source rows (no
 * block stride). Y identity preserves the dual-ISO 4-row pattern
 * unchanged. */
int pl_downsample_bayer_to_bayer_4x_x_only(const uint16_t * bayer_in,
                                            int in_w,
                                            int in_h,
                                            uint16_t * bayer_out,
                                            int * out_w_p,
                                            int * out_h_p,
                                            int threads)
{
    if (!bayer_in || !bayer_out) return 1;
    if (in_w < 4 || in_h < 1) return 1;
    if (in_w & 3) return 1;

    const int out_w = in_w >> 2;
    const int out_h = in_h;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const uint16_t * __restrict srow = bayer_in + (size_t)y_out * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const uint16_t * __restrict srow = bayer_in + (size_t)y_out * (size_t)in_w;
            uint16_t * __restrict drow = bayer_out + (size_t)y_out * (size_t)out_w;
            for (int x_out = 0; x_out + 1 < out_w; x_out += 2)
            {
                const int xs = x_out * 4;
                const uint32_t r0 = srow[xs + 0];
                const uint32_t r1 = srow[xs + 2];
                const uint32_t g0 = srow[xs + 1];
                const uint32_t g1 = srow[xs + 3];
                drow[x_out + 0] = (uint16_t)((r0 + r1) >> 1);
                drow[x_out + 1] = (uint16_t)((g0 + g1) >> 1);
            }
        }
    }
    return 0;
}

/* RGB→RGB Y-only 4x downsample. Each output RGB pixel = mean of 4
 * vertical RGB neighbors at the same X. Threads parallelise over output
 * rows. */
int pl_downsample_rgb_to_rgb_4x_y_only(const uint16_t * in_rgb,
                                       int in_w,
                                       int in_h,
                                       uint16_t * out_rgb,
                                       int * out_w_p,
                                       int * out_h_p,
                                       int threads)
{
    if (!in_rgb || !out_rgb) return 1;
    if (in_w < 1 || in_h < 4) return 1;
    if (in_h & 3) return 1;

    const int out_w = in_w;
    const int out_h = in_h >> 2;
    if (out_w_p) *out_w_p = out_w;
    if (out_h_p) *out_h_p = out_h;

    const size_t row_stride = (size_t)in_w * 3u;

    if (threads > 1)
    {
        #pragma omp parallel for num_threads(threads)
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int y_src = y_out * 4;
            const uint16_t * __restrict r0 = in_rgb + (size_t)y_src * row_stride;
            const uint16_t * __restrict r1 = r0 + row_stride;
            const uint16_t * __restrict r2 = r1 + row_stride;
            const uint16_t * __restrict r3 = r2 + row_stride;
            uint16_t * __restrict drow = out_rgb + (size_t)y_out * row_stride;
            for (int i = 0; i < (int)row_stride; ++i)
            {
                const uint32_t s = (uint32_t)r0[i] + (uint32_t)r1[i]
                                 + (uint32_t)r2[i] + (uint32_t)r3[i];
                drow[i] = (uint16_t)(s >> 2);
            }
        }
    }
    else
    {
        for (int y_out = 0; y_out < out_h; ++y_out)
        {
            const int y_src = y_out * 4;
            const uint16_t * __restrict r0 = in_rgb + (size_t)y_src * row_stride;
            const uint16_t * __restrict r1 = r0 + row_stride;
            const uint16_t * __restrict r2 = r1 + row_stride;
            const uint16_t * __restrict r3 = r2 + row_stride;
            uint16_t * __restrict drow = out_rgb + (size_t)y_out * row_stride;
            for (int i = 0; i < (int)row_stride; ++i)
            {
                const uint32_t s = (uint32_t)r0[i] + (uint32_t)r1[i]
                                 + (uint32_t)r2[i] + (uint32_t)r3[i];
                drow[i] = (uint16_t)(s >> 2);
            }
        }
    }
    return 0;
}
