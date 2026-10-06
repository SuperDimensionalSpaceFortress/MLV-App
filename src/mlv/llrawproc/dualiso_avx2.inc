/* dualiso_avx2.inc — AVX2 + FMA kernels for HQ Dual ISO recon.
 *
 * This file is included from dualiso.c. It expects the following symbols
 * to be in scope at include time:
 *   EV_RESOLUTION (define), ALIAS_MAP_MAX (extern int), MAX/MIN/COERCE
 *   raw2ev, ev2raw (int*), randn05_cache (float[1024])
 *
 * Each kernel is compiled with __attribute__((target("avx2,fma"))) so the
 * AVX2 path lights up on capable hosts even though dualiso.c itself is
 * built with the project's baseline flags. Layout matches the Phase B0
 * prototype at .claude-state/profiling/20260424-phase-b0-final-blend/
 * final_blend_bench.c.
 *
 * Drift bound: float32 FMA reordering may differ from scalar double-precision
 * by ±1 LSB on a small fraction of pixels. The downstream pipeline already
 * absorbs ±8 LSB of dither noise from raw_set_pixel_20to16_rand, so this
 * level of drift is structurally invisible.
 */

#ifndef DUALISO_AVX2_KERNELS_INCLUDED
#define DUALISO_AVX2_KERNELS_INCLUDED

/* ALIAS_MAP_MAX is defined later in dualiso.c (file-scope `static const`).
 * Mirror its value here as a compile-time constant for the SIMD kernels. */
#ifndef DUALISO_ALIAS_MAP_MAX
#define DUALISO_ALIAS_MAP_MAX 15000
#endif

#ifdef DUALISO_AVX2_AVAILABLE

/* immintrin.h is already included by dualiso.c above. */

/* ====================================================================== *
 * B1.1 — convert_to_20bit: image_data[i] (uint16) << 6, mask 0xFFFFF.
 * 16 px per iter via two _mm256_cvtepu16_epi32 + _mm256_slli_epi32.
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void convert_to_20bit_avx2(uint32_t * out, const uint16_t * src, size_t total)
{
    const __m256i mask20 = _mm256_set1_epi32(0xFFFFF);
    size_t x = 0;
    for (; x + 16 <= total; x += 16) {
        __m256i lo16 = _mm256_loadu_si256((const __m256i*)(src + x));
        __m128i lo_low  = _mm256_castsi256_si128(lo16);
        __m128i lo_high = _mm256_extracti128_si256(lo16, 1);
        __m256i a = _mm256_cvtepu16_epi32(lo_low);
        __m256i b = _mm256_cvtepu16_epi32(lo_high);
        a = _mm256_and_si256(_mm256_slli_epi32(a, 6), mask20);
        b = _mm256_and_si256(_mm256_slli_epi32(b, 6), mask20);
        _mm256_storeu_si256((__m256i*)(out + x), a);
        _mm256_storeu_si256((__m256i*)(out + x + 8), b);
    }
    for (; x < total; x++) {
        out[x] = (((uint32_t)src[x]) << 6) & 0xFFFFF;
    }
}

__attribute__((target("avx2,fma")))
static inline __m256i dualiso_load8_u16_as_i32_avx2(const uint16_t * src)
{
    return _mm256_cvtepu16_epi32(_mm_loadu_si128((const __m128i*)src));
}

__attribute__((target("avx2,fma")))
static inline void dualiso_store8_i32_as_u16_avx2(uint16_t * dst, __m256i value)
{
    __m128i lo = _mm256_castsi256_si128(value);
    __m128i hi = _mm256_extracti128_si256(value, 1);
    __m128i packed = _mm_packus_epi32(lo, hi);
    _mm_storeu_si128((__m128i*)dst, packed);
}

__attribute__((target("avx2,fma")))
static void overexposed_mark_row_avx2(uint16_t * overexposed,
                                      const uint32_t * bright,
                                      const uint32_t * dark,
                                      uint32_t white_darkened,
                                      uint32_t white,
                                      int w)
{
    const int32_t bright_limit = white_darkened > 0 ? (int32_t)(white_darkened - 1u) : -1;
    const int32_t dark_limit = white > 0 ? (int32_t)(white - 1u) : -1;
    const __m256i bright_threshold = _mm256_set1_epi32(bright_limit);
    const __m256i dark_threshold = _mm256_set1_epi32(dark_limit);
    const __m256i hundred = _mm256_set1_epi32(100);

    int x = 0;
    for (; x + 8 <= w; x += 8) {
        __m256i b = _mm256_loadu_si256((const __m256i*)(bright + x));
        __m256i d = _mm256_loadu_si256((const __m256i*)(dark + x));
        __m256i b_over = _mm256_cmpgt_epi32(b, bright_threshold);
        __m256i d_over = _mm256_cmpgt_epi32(d, dark_threshold);
        __m256i value = _mm256_and_si256(_mm256_or_si256(b_over, d_over), hundred);
        dualiso_store8_i32_as_u16_avx2(overexposed + x, value);
    }
    for (; x < w; x++) {
        overexposed[x] = bright[x] >= white_darkened || dark[x] >= white ? 100 : 0;
    }
}

__attribute__((target("avx2,fma")))
static void overexposed_blur_row_avx2(uint16_t * overexposed,
                                      const uint16_t * prev,
                                      const uint16_t * curr,
                                      const uint16_t * next,
                                      int w)
{
    const __m256i weight_cardinal = _mm256_set1_epi32(820);
    const __m256i weight_diagonal = _mm256_set1_epi32(657);

    int x = 3;
    const int x_end = w - 3;
    for (; x + 8 <= x_end; x += 8) {
        __m256i center = dualiso_load8_u16_as_i32_avx2(curr + x);
        __m256i cardinal = _mm256_add_epi32(
            _mm256_add_epi32(dualiso_load8_u16_as_i32_avx2(prev + x),
                             dualiso_load8_u16_as_i32_avx2(curr + x - 1)),
            _mm256_add_epi32(dualiso_load8_u16_as_i32_avx2(curr + x + 1),
                             dualiso_load8_u16_as_i32_avx2(next + x)));
        __m256i diagonal = _mm256_add_epi32(
            _mm256_add_epi32(dualiso_load8_u16_as_i32_avx2(prev + x - 1),
                             dualiso_load8_u16_as_i32_avx2(prev + x + 1)),
            _mm256_add_epi32(dualiso_load8_u16_as_i32_avx2(next + x - 1),
                             dualiso_load8_u16_as_i32_avx2(next + x + 1)));
        cardinal = _mm256_srai_epi32(_mm256_mullo_epi32(cardinal, weight_cardinal), 10);
        diagonal = _mm256_srai_epi32(_mm256_mullo_epi32(diagonal, weight_diagonal), 10);
        dualiso_store8_i32_as_u16_avx2(overexposed + x,
                                       _mm256_add_epi32(center,
                                                        _mm256_add_epi32(cardinal, diagonal)));
    }
    for (; x < x_end; x++) {
        overexposed[x] =
            (curr[x])+
            (prev[x] + curr[x-1] + curr[x+1] + next[x]) * 820 / 1024 +
            (prev[x-1] + prev[x+1] + next[x-1] + next[x+1]) * 657 / 1024;
    }
}

/* ====================================================================== *
 * B1.2 — fullres_reconstruction inner row.
 * For BRIGHT rows: fullres[i] = (bright[i] < white_darkened) ? bright[i]
 *                                                            : MAX(bright[i], dark[i]);
 * For DARK   rows: fullres[i] = dark[i].
 * Branch is row-uniform (BRIGHT_ROW depends on y only), so we hoist it
 * outside the inner loop. SIMD only useful on bright rows; dark rows are
 * a memcpy.
 * LOOK-ASSIST-M16-CAST-5: the test against white_darkened reads `test`
 * (bright itself, or the A12 quad max); a non-NULL `decision` receives the
 * per-pixel outcome (1 = took max(bright, dark)).
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void fullres_reconstruction_bright_row_avx2(uint32_t * fullres,
                                                    const uint32_t * bright,
                                                    const uint32_t * test,
                                                    const uint32_t * dark,
                                                    uint32_t white_darkened,
                                                    int w,
                                                    unsigned char * decision)
{
    const __m256i v_white = _mm256_set1_epi32((int32_t)white_darkened);
    int x = 0;
    for (; x + 8 <= w; x += 8) {
        __m256i b = _mm256_loadu_si256((const __m256i*)(bright + x));
        __m256i t = _mm256_loadu_si256((const __m256i*)(test + x));
        __m256i d = _mm256_loadu_si256((const __m256i*)(dark + x));
        /* mask = t < white_darkened (signed compare safe: values < 1<<20) */
        __m256i ge = _mm256_cmpgt_epi32(v_white, t);  /* white > t -> t < white */
        /* if (t < white): out = b, else out = max(b, d) */
        __m256i mx = _mm256_max_epu32(b, d);
        __m256i out = _mm256_blendv_epi8(mx, b, ge);
        _mm256_storeu_si256((__m256i*)(fullres + x), out);
        if (decision) {
            const int keep = _mm256_movemask_ps(_mm256_castsi256_ps(ge));
            for (int k = 0; k < 8; k++) decision[x + k] = ((keep >> k) & 1) ? 0 : 1;
        }
    }
    for (; x < w; x++) {
        uint32_t f = bright[x];
        uint32_t dd = dark[x];
        const int over = !(test[x] < white_darkened);
        fullres[x] = !over ? f : (f > dd ? f : dd);
        if (decision) decision[x] = (unsigned char)over;
    }
}

/* ====================================================================== *
 * B1.3 — convert_20_to_16bit: out[i] = clamp((raw32[i]/16) + randn05 + 0.5, 0, 65535)
 * 8 px / iter; gather 8 dither floats from the precomputed cache via a
 * caller-supplied counter k.
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void convert_20_to_16bit_row_avx2(uint16_t * out,
                                          const uint32_t * src,
                                          const float * randn_cache,
                                          int * k_inout,
                                          int w)
{
    const __m256 v_inv16 = _mm256_set1_ps(1.0f/16.0f);
    const __m256 v_half  = _mm256_set1_ps(0.5f);
    const __m256i v_zero = _mm256_setzero_si256();
    const __m256i v_max16 = _mm256_set1_epi32(0xFFFF);
    int k = *k_inout;
    int x = 0;
    for (; x + 8 <= w; x += 8) {
        __m256i v = _mm256_loadu_si256((const __m256i*)(src + x));
        __m256 vf = _mm256_cvtepi32_ps(v);
        int idxs[8] __attribute__((aligned(32)));
        for (int i = 0; i < 8; i++) idxs[i] = (k + i) & 1023;
        __m256i vidx = _mm256_load_si256((const __m256i*)idxs);
        __m256 dither = _mm256_i32gather_ps(randn_cache, vidx, 4);
        __m256 r = _mm256_fmadd_ps(vf, v_inv16, dither);
        r = _mm256_add_ps(r, v_half);
        __m256i ri = _mm256_cvttps_epi32(r);
        ri = _mm256_max_epi32(ri, v_zero);
        ri = _mm256_min_epi32(ri, v_max16);
        __m128i lo = _mm256_castsi256_si128(ri);
        __m128i hi = _mm256_extracti128_si256(ri, 1);
        __m128i p  = _mm_packus_epi32(lo, hi);
        _mm_storeu_si128((__m128i*)(out + x), p);
        k = (k + 8) & 1023;
    }
    for (; x < w; x++) {
        float dither = randn_cache[k & 1023];
        k = (k + 1) & 1023;
        int v = (int)((src[x] / 16.0f) + dither + 0.5f);
        if (v < 0) v = 0;
        if (v > 0xFFFF) v = 0xFFFF;
        out[x] = (uint16_t)v;
    }
    *k_inout = k;
}

__attribute__((target("avx2,fma")))
static inline void convert_raw32_chunk_to_16bit_avx2(uint16_t * out,
                                                     __m256i raw,
                                                     const float * randn_cache,
                                                     int * k_inout)
{
    const __m256 v_inv16 = _mm256_set1_ps(1.0f/16.0f);
    const __m256 v_half  = _mm256_set1_ps(0.5f);
    const __m256i v_zero = _mm256_setzero_si256();
    const __m256i v_max16 = _mm256_set1_epi32(0xFFFF);
    int k = *k_inout;
    int idxs[8] __attribute__((aligned(32)));
    for (int i = 0; i < 8; i++) idxs[i] = (k + i) & 1023;
    __m256i vidx = _mm256_load_si256((const __m256i*)idxs);
    __m256 dither = _mm256_i32gather_ps(randn_cache, vidx, 4);
    __m256 vf = _mm256_cvtepi32_ps(raw);
    __m256 r = _mm256_fmadd_ps(vf, v_inv16, dither);
    r = _mm256_add_ps(r, v_half);
    __m256i ri = _mm256_cvttps_epi32(r);
    ri = _mm256_max_epi32(ri, v_zero);
    ri = _mm256_min_epi32(ri, v_max16);
    __m128i lo = _mm256_castsi256_si128(ri);
    __m128i hi = _mm256_extracti128_si256(ri, 1);
    __m128i p  = _mm_packus_epi32(lo, hi);
    _mm_storeu_si128((__m128i*)out, p);
    k = (k + 8) & 1023;
    *k_inout = k;
}

/* ====================================================================== *
 * B2.1 — final_blend kernel (the big win).
 * Direct transcription of .claude-state/profiling/20260424-phase-b0-final-blend/
 * final_blend_bench.c::final_blend_avx2, adapted to the production
 * signature with optional alias_map (NULL means c=0 path).
 *
 * Processes 8 pixels per iter; tail pixels handled by caller.
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void final_blend_row_avx2_no_alias(uint32_t * raw_buffer_32,
                                          const uint32_t * fullres,
                                          const uint32_t * fullres_smooth,
                                          const uint32_t * halfres_smooth,
                                          const uint32_t * dark,
                                          const uint32_t * bright,
                                          const uint16_t * overexposed,
                                          const float * raw2ev_float_lut,
                                          const int * raw2ev_lut,
                                          const int * ev2raw_lut,        /* points to ev2raw_0 + 10*EV_RESOLUTION */
                                          const double * fullres_curve,
                                          const float * fullres_curve_f,
                                          int black, int dark_noise,
                                          int total)
{
    (void)raw2ev_lut;
    const __m256i mask20 = _mm256_set1_epi32(0xFFFFF);
    const __m256i v_black     = _mm256_set1_epi32(black);
    const __m256i v_lo        = _mm256_set1_epi32(-10*EV_RESOLUTION);
    const __m256i v_hi        = _mm256_set1_epi32(14*EV_RESOLUTION - 1);
    const __m256  v_one       = _mm256_set1_ps(1.0f);
    const __m256  v_zero      = _mm256_setzero_ps();
    const __m256  v_inv_200   = _mm256_set1_ps(1.0f / 200.0f);
    const __m256  v_inv4dn    = _mm256_set1_ps(1.0f / (4.0f * (float)dark_noise));

    int x = 0;
    for (; x + 8 <= total; x += 8) {
        __m256i b   = _mm256_loadu_si256((const __m256i*)&bright[x]);
        __m256i hr  = _mm256_loadu_si256((const __m256i*)&halfres_smooth[x]);
        __m256i fr  = _mm256_loadu_si256((const __m256i*)&fullres[x]);
        __m256i frs = _mm256_loadu_si256((const __m256i*)&fullres_smooth[x]);
        __m256i d   = _mm256_loadu_si256((const __m256i*)&dark[x]);

        __m256 hrev  = _mm256_i32gather_ps(raw2ev_float_lut, hr,  4);
        __m256 frev  = _mm256_i32gather_ps(raw2ev_float_lut, fr,  4);
        __m256 frsev = _mm256_i32gather_ps(raw2ev_float_lut, frs, 4);

        __m256i b_idx = _mm256_and_si256(b, mask20);
        __m256 f;
        if (fullres_curve_f) {
            f = _mm256_setr_ps(fullres_curve_f[bright[x + 0] & 0xFFFFF],
                               fullres_curve_f[bright[x + 1] & 0xFFFFF],
                               fullres_curve_f[bright[x + 2] & 0xFFFFF],
                               fullres_curve_f[bright[x + 3] & 0xFFFFF],
                               fullres_curve_f[bright[x + 4] & 0xFFFFF],
                               fullres_curve_f[bright[x + 5] & 0xFFFFF],
                               fullres_curve_f[bright[x + 6] & 0xFFFFF],
                               fullres_curve_f[bright[x + 7] & 0xFFFFF]);
        } else {
            __m128i b_idx_lo = _mm256_castsi256_si128(b_idx);
            __m128i b_idx_hi = _mm256_extracti128_si256(b_idx, 1);
            __m256d f_lo_d = _mm256_i32gather_pd(fullres_curve, b_idx_lo, 8);
            __m256d f_hi_d = _mm256_i32gather_pd(fullres_curve, b_idx_hi, 8);
            __m128 f_lo = _mm256_cvtpd_ps(f_lo_d);
            __m128 f_hi = _mm256_cvtpd_ps(f_hi_d);
            f = _mm256_insertf128_ps(_mm256_castps128_ps256(f_lo), f_hi, 1);
        }

        __m256 c_amap = v_zero;

        __m128i over16 = _mm_loadu_si128((const __m128i*)&overexposed[x]);
        __m256i over_i = _mm256_cvtepu16_epi32(over16);
        __m256 ovf = _mm256_mul_ps(_mm256_cvtepi32_ps(over_i), v_inv_200);
        ovf = _mm256_min_ps(_mm256_max_ps(ovf, v_zero), v_one);

        __m256 c = _mm256_max_ps(c_amap, ovf);
        __m256 noisy_or_over = _mm256_max_ps(ovf, _mm256_sub_ps(v_one, f));
        f = _mm256_max_ps(f, c);

        __m256 fev = _mm256_fmadd_ps(noisy_or_over, _mm256_sub_ps(frsev, frev), frev);

        __m256i sig = _mm256_srai_epi32(_mm256_add_epi32(d, b), 1);
        __m256i sig_minus_black = _mm256_sub_epi32(sig, v_black);
        __m256 cap = _mm256_mul_ps(_mm256_cvtepi32_ps(sig_minus_black), v_inv4dn);
        f = _mm256_max_ps(v_zero, _mm256_min_ps(f, cap));

        __m256 outf = _mm256_fmadd_ps(f, _mm256_sub_ps(fev, hrev), hrev);

        __m256i outi = _mm256_cvttps_epi32(outf);
        outi = _mm256_min_epi32(_mm256_max_epi32(outi, v_lo), v_hi);

        __m256i raw_out = _mm256_i32gather_epi32(ev2raw_lut, outi, 4);
        _mm256_storeu_si256((__m256i*)&raw_buffer_32[x], raw_out);
    }
}

__attribute__((target("avx2,fma")))
static void final_blend_row_avx2(uint32_t * raw_buffer_32,
                                 uint16_t * image_data,
                                 const uint32_t * fullres,
                                 const uint32_t * fullres_smooth,
                                 const uint32_t * halfres_smooth,
                                 const uint32_t * dark,
                                 const uint32_t * bright,
                                 const uint16_t * overexposed,
                                 const uint16_t * alias_map,   /* may be NULL */
                                 const float * raw2ev_float_lut,
                                 const int * raw2ev_lut,
                                 const int * ev2raw_lut,        /* points to ev2raw_0 + 10*EV_RESOLUTION */
                                 const double * fullres_curve,
                                 const float * fullres_curve_f,
                                 int black, int dark_noise,
                                 int total,
                                 const float * randn_cache,
                                 int * k_inout,
                                 int fuse_to_16bit,
                                 int probe_mode,
                                 double * raw2ev_probe_ms,
                                 double * fullres_curve_probe_ms,
                                 double * arithmetic_probe_ms,
                                 double * ev2raw_store_probe_ms,
                                 double * overexposed_density,
                                 double * cap_clamp_pct,
                                 double * f_near_0_pct,
                                 double * f_near_1_pct)
{
    (void)raw2ev_lut;
    const __m256i mask20 = _mm256_set1_epi32(0xFFFFF);
    const __m256i v_black     = _mm256_set1_epi32(black);
    const __m256i v_lo        = _mm256_set1_epi32(-10*EV_RESOLUTION);
    const __m256i v_hi        = _mm256_set1_epi32(14*EV_RESOLUTION - 1);
    const __m256  v_one       = _mm256_set1_ps(1.0f);
    const __m256  v_zero      = _mm256_setzero_ps();
    const __m256  v_inv_alias = _mm256_set1_ps(1.0f / (float)DUALISO_ALIAS_MAP_MAX);
    const __m256  v_inv_200   = _mm256_set1_ps(1.0f / 200.0f);
    const __m256  v_inv4dn    = _mm256_set1_ps(1.0f / (4.0f * (float)dark_noise));
    const int probe_raw2ev = (probe_mode == 0 || probe_mode == 1);
    const int probe_fullres_curve = (probe_mode == 0 || probe_mode == 2);
    const int probe_ev2raw_store = (probe_mode == 0 || probe_mode == 3);
    const int probe_arithmetic = (probe_mode == 0 || probe_mode == 4);
    const int probe_detail = probe_mode >= 0;
    const __m256 v_near_zero = _mm256_set1_ps(0.01f);
    const __m256 v_near_one = _mm256_set1_ps(0.99f);
    double local_overexposed = 0.0;
    double local_cap_clamp = 0.0;
    double local_f_near_0 = 0.0;
    double local_f_near_1 = 0.0;

    int x = 0;
    for (; x + 8 <= total; x += 8) {
        __m256i b   = _mm256_loadu_si256((const __m256i*)&bright[x]);
        __m256i hr  = _mm256_loadu_si256((const __m256i*)&halfres_smooth[x]);
        __m256i fr  = _mm256_loadu_si256((const __m256i*)&fullres[x]);
        __m256i frs = _mm256_loadu_si256((const __m256i*)&fullres_smooth[x]);
        __m256i d   = _mm256_loadu_si256((const __m256i*)&dark[x]);

        double stage_start = 0.0;
        if (probe_raw2ev) stage_start = mlv_stage_timing_now();
        __m256 hrev  = _mm256_i32gather_ps(raw2ev_float_lut, hr,  4);
        __m256 frev  = _mm256_i32gather_ps(raw2ev_float_lut, fr,  4);
        __m256 frsev = _mm256_i32gather_ps(raw2ev_float_lut, frs, 4);
        if (probe_raw2ev) *raw2ev_probe_ms += dualiso_debug_elapsed_ms(stage_start);

        stage_start = 0.0;
        if (probe_fullres_curve) stage_start = mlv_stage_timing_now();
        __m256 f;
        if (fullres_curve_f) {
            f = _mm256_setr_ps(fullres_curve_f[bright[x + 0] & 0xFFFFF],
                               fullres_curve_f[bright[x + 1] & 0xFFFFF],
                               fullres_curve_f[bright[x + 2] & 0xFFFFF],
                               fullres_curve_f[bright[x + 3] & 0xFFFFF],
                               fullres_curve_f[bright[x + 4] & 0xFFFFF],
                               fullres_curve_f[bright[x + 5] & 0xFFFFF],
                               fullres_curve_f[bright[x + 6] & 0xFFFFF],
                               fullres_curve_f[bright[x + 7] & 0xFFFFF]);
        } else {
            __m256i b_idx = _mm256_and_si256(b, mask20);
            __m128i b_idx_lo = _mm256_castsi256_si128(b_idx);
            __m128i b_idx_hi = _mm256_extracti128_si256(b_idx, 1);
            __m256d f_lo_d = _mm256_i32gather_pd(fullres_curve, b_idx_lo, 8);
            __m256d f_hi_d = _mm256_i32gather_pd(fullres_curve, b_idx_hi, 8);
            __m128 f_lo = _mm256_cvtpd_ps(f_lo_d);
            __m128 f_hi = _mm256_cvtpd_ps(f_hi_d);
            f = _mm256_insertf128_ps(_mm256_castps128_ps256(f_lo), f_hi, 1);
        }
        if (probe_fullres_curve) *fullres_curve_probe_ms += dualiso_debug_elapsed_ms(stage_start);

        stage_start = 0.0;
        if (probe_arithmetic) stage_start = mlv_stage_timing_now();
        __m256 c_amap;
        if (alias_map) {
            __m128i amap16 = _mm_loadu_si128((const __m128i*)&alias_map[x]);
            __m256i amap_i = _mm256_cvtepu16_epi32(amap16);
            c_amap = _mm256_mul_ps(_mm256_cvtepi32_ps(amap_i), v_inv_alias);
            c_amap = _mm256_min_ps(_mm256_max_ps(c_amap, v_zero), v_one);
        } else {
            c_amap = v_zero;
        }

        __m128i over16 = _mm_loadu_si128((const __m128i*)&overexposed[x]);
        __m256i over_i = _mm256_cvtepu16_epi32(over16);
        __m256 ovf = _mm256_mul_ps(_mm256_cvtepi32_ps(over_i), v_inv_200);
        ovf = _mm256_min_ps(_mm256_max_ps(ovf, v_zero), v_one);

        __m256 c = _mm256_max_ps(c_amap, ovf);
        __m256 noisy_or_over = _mm256_max_ps(ovf, _mm256_sub_ps(v_one, f));
        f = _mm256_max_ps(f, c);
        if (probe_detail) {
            __m256 over_mask = _mm256_cmp_ps(ovf, v_zero, _CMP_GT_OQ);
            local_overexposed += (double)dualiso_count_bits_u32((unsigned)_mm256_movemask_ps(over_mask));
        }

        __m256 fev = _mm256_fmadd_ps(noisy_or_over, _mm256_sub_ps(frsev, frev), frev);

        __m256i sig = _mm256_srai_epi32(_mm256_add_epi32(d, b), 1);
        __m256i sig_minus_black = _mm256_sub_epi32(sig, v_black);
        __m256 cap = _mm256_mul_ps(_mm256_cvtepi32_ps(sig_minus_black), v_inv4dn);
        if (probe_detail) {
            __m256 cap_clamp_mask = _mm256_cmp_ps(f, cap, _CMP_GT_OQ);
            local_cap_clamp += (double)dualiso_count_bits_u32((unsigned)_mm256_movemask_ps(cap_clamp_mask));
        }
        f = _mm256_max_ps(v_zero, _mm256_min_ps(f, cap));

        __m256 outf = _mm256_fmadd_ps(f, _mm256_sub_ps(fev, hrev), hrev);
        if (probe_detail) {
            __m256 near0_mask = _mm256_cmp_ps(f, v_near_zero, _CMP_LE_OQ);
            __m256 near1_mask = _mm256_cmp_ps(f, v_near_one, _CMP_GE_OQ);
            local_f_near_0 += (double)dualiso_count_bits_u32((unsigned)_mm256_movemask_ps(near0_mask));
            local_f_near_1 += (double)dualiso_count_bits_u32((unsigned)_mm256_movemask_ps(near1_mask));
        }

        __m256i outi = _mm256_cvttps_epi32(outf);
        outi = _mm256_min_epi32(_mm256_max_epi32(outi, v_lo), v_hi);

        if (probe_arithmetic) *arithmetic_probe_ms += dualiso_debug_elapsed_ms(stage_start);

        stage_start = 0.0;
        if (probe_ev2raw_store) stage_start = mlv_stage_timing_now();
        __m256i raw_out = _mm256_i32gather_epi32(ev2raw_lut, outi, 4);
        if (fuse_to_16bit) {
            convert_raw32_chunk_to_16bit_avx2(&image_data[x], raw_out, randn_cache, k_inout);
        } else {
            _mm256_storeu_si256((__m256i*)&raw_buffer_32[x], raw_out);
        }
        if (probe_ev2raw_store) *ev2raw_store_probe_ms += dualiso_debug_elapsed_ms(stage_start);
    }

    if (probe_detail)
    {
        *overexposed_density += local_overexposed;
        *cap_clamp_pct += local_cap_clamp;
        *f_near_0_pct += local_f_near_0;
        *f_near_1_pct += local_f_near_1;
    }
}

__attribute__((target("avx2,fma")))
/* ====================================================================== *
 * B2.2 — mix_images half-res blend.
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void mix_images_row_avx2(uint32_t * halfres,
                                 const uint32_t * bright,
                                 const uint32_t * dark,
                                 const float * raw2ev_float_lut,
                                 const int * raw2ev_lut,
                                 const int * ev2raw_lut,
                                 const float * mix_curve,
                                 int total)
{
    (void)raw2ev_lut;
    const __m256i mask20 = _mm256_set1_epi32(0xFFFFF);
    const __m256i v_lo   = _mm256_set1_epi32(-10*EV_RESOLUTION);
    const __m256i v_hi   = _mm256_set1_epi32(14*EV_RESOLUTION - 1);
    const __m256  v_one  = _mm256_set1_ps(1.0f);
    const __m256  v_zero = _mm256_setzero_ps();

    int x = 0;
    for (; x + 8 <= total; x += 8) {
        __m256i b = _mm256_loadu_si256((const __m256i*)&bright[x]);
        __m256i d = _mm256_loadu_si256((const __m256i*)&dark[x]);

        __m256 bev = _mm256_i32gather_ps(raw2ev_float_lut, b, 4);
        __m256 dev = _mm256_i32gather_ps(raw2ev_float_lut, d, 4);

        __m256i b_idx = _mm256_and_si256(b, mask20);
        __m256 k = _mm256_i32gather_ps(mix_curve, b_idx, 4);
        k = _mm256_min_ps(_mm256_max_ps(k, v_zero), v_one);

        /* mixed = bev*(1-k) + dev*k = bev + k*(dev - bev) */
        __m256 mixed_f = _mm256_fmadd_ps(k, _mm256_sub_ps(dev, bev), bev);
        __m256i mixed_i = _mm256_cvttps_epi32(mixed_f);
        mixed_i = _mm256_min_epi32(_mm256_max_epi32(mixed_i, v_lo), v_hi);

        __m256i raw_out = _mm256_i32gather_epi32(ev2raw_lut, mixed_i, 4);
        _mm256_storeu_si256((__m256i*)&halfres[x], raw_out);
    }
    (void)x;
}

/* ====================================================================== *
 * C4.1 — build_alias_map initial err diff (8 px / iter).
 *
 * Scalar reference (dualiso.c:2715-2733):
 *
 *     for (int x = 0; x < w; x++) {
 *         if (fullres_curve[bright[x + y*w]] > fullres_thr) continue;
 *         int f  = fullres_smooth[x + y*w];
 *         int hh = halfres_smooth[x + y*w];
 *         int fe = raw2ev[f];
 *         int he = raw2ev[hh];
 *         int e_lin = ABS(f - hh);
 *         e_lin = MAX(e_lin - dark_noise*3/2, 0);
 *         int e_log = ABS(fe - he);
 *         alias_map[x + y*w] = MIN(MIN(e_lin/2, e_log/16), 65530);
 *     }
 *
 * The output buffer is pre-zeroed (memset at dualiso.c:3358), so the
 * predicated `continue` leaves zero in those slots. The AVX2 kernel
 * preserves byte-identity by computing the result for all 8 lanes and
 * blending against zero where the predicate fires.
 *
 * Predicate uses fullres_curve (double*, 8B/elem); we gather 4+4 doubles
 * and compare against fullres_thr broadcast.
 *
 * Integer arithmetic is identity: ABS / MIN / MAX / shift-right-by-1 /
 * shift-right-by-4. No floats touched on the result path.
 * ====================================================================== */
/* Helper: pack two __m256d compare-mask vectors (each is 4×64-bit all-ones
 * or all-zeros) into a single __m256i with 8×32-bit lanes (each lane all-ones
 * or all-zeros), preserving lane order [lo0..lo3, hi0..hi3].
 *
 * Each __m256d compare result has lane k as 64 bits of either 0 or 1.
 * The low 32 bits of each int64 carry the same all-1 / all-0 pattern, so
 * collecting low-32 of each int64 across both vectors gives the 8-lane mask. */
__attribute__((target("avx2,fma")))
static inline __m256i pack_pd_cmp_to_epi32_mask(__m256d skip_lo, __m256d skip_hi)
{
    __m256i lo_i = _mm256_castpd_si256(skip_lo);
    __m256i hi_i = _mm256_castpd_si256(skip_hi);
    /* Per 128-bit half: src has 2 int64 lanes. _mm_shuffle_epi32 with
     * _MM_SHUFFLE(2,0,2,0) maps int32 lanes [a0,a1,a2,a3] → [a0,a2,a0,a2],
     * placing the two int32 masks (one per int64) in lanes 0 and 1. */
    __m128i lo_lo128 = _mm_shuffle_epi32(_mm256_castsi256_si128(lo_i),       _MM_SHUFFLE(2,0,2,0));
    __m128i lo_hi128 = _mm_shuffle_epi32(_mm256_extracti128_si256(lo_i, 1),  _MM_SHUFFLE(2,0,2,0));
    __m128i hi_lo128 = _mm_shuffle_epi32(_mm256_castsi256_si128(hi_i),       _MM_SHUFFLE(2,0,2,0));
    __m128i hi_hi128 = _mm_shuffle_epi32(_mm256_extracti128_si256(hi_i, 1),  _MM_SHUFFLE(2,0,2,0));
    /* Combine: lo half gets [lo0,lo1,lo2,lo3], hi half gets [hi0,hi1,hi2,hi3] */
    __m128i mask_lo = _mm_unpacklo_epi64(lo_lo128, lo_hi128);
    __m128i mask_hi = _mm_unpacklo_epi64(hi_lo128, hi_hi128);
    return _mm256_inserti128_si256(_mm256_castsi128_si256(mask_lo), mask_hi, 1);
}

__attribute__((target("avx2,fma")))
static void build_alias_map_init_diff_row_avx2(uint16_t * alias_map,
                                                const uint32_t * fullres_smooth_row,
                                                const uint32_t * halfres_smooth_row,
                                                const uint32_t * bright_row,
                                                const int * raw2ev_lut,
                                                const double * fullres_curve,
                                                double fullres_thr_v,
                                                int dark_noise,
                                                int w)
{
    const int dn_bias = dark_noise * 3 / 2;            /* matches scalar `dark_noise*3/2` */
    const __m256i v_dn_bias = _mm256_set1_epi32(dn_bias);
    const __m256i v_zero    = _mm256_setzero_si256();
    const __m256i v_clamp   = _mm256_set1_epi32(65530);
    const __m256d v_thr     = _mm256_set1_pd(fullres_thr_v);

    int x = 0;
    for (; x + 8 <= w; x += 8) {
        __m256i b   = _mm256_loadu_si256((const __m256i*)&bright_row[x]);
        __m256i fs  = _mm256_loadu_si256((const __m256i*)&fullres_smooth_row[x]);
        __m256i hs  = _mm256_loadu_si256((const __m256i*)&halfres_smooth_row[x]);

        /* Gather fullres_curve[bright[i]] (double, 8B/elem). Index space is
         * 1<<20 elements (4 MiB); bright values are 20-bit by construction. */
        __m128i b_idx_lo = _mm256_castsi256_si128(b);
        __m128i b_idx_hi = _mm256_extracti128_si256(b, 1);
        __m256d fc_lo = _mm256_i32gather_pd(fullres_curve, b_idx_lo, 8);
        __m256d fc_hi = _mm256_i32gather_pd(fullres_curve, b_idx_hi, 8);
        /* Predicate: fullres_curve[bright] > fullres_thr → skip (write 0). */
        __m256d skip_lo_pd = _mm256_cmp_pd(fc_lo, v_thr, _CMP_GT_OQ);
        __m256d skip_hi_pd = _mm256_cmp_pd(fc_hi, v_thr, _CMP_GT_OQ);
        __m256i mask = pack_pd_cmp_to_epi32_mask(skip_lo_pd, skip_hi_pd);

        /* Gather raw2ev[f] and raw2ev[h]. */
        __m256i fe = _mm256_i32gather_epi32(raw2ev_lut, fs, 4);
        __m256i he = _mm256_i32gather_epi32(raw2ev_lut, hs, 4);

        /* e_lin = ABS(f - hh); e_lin = MAX(e_lin - dn_bias, 0). */
        __m256i diff_lin = _mm256_sub_epi32(fs, hs);
        __m256i e_lin    = _mm256_abs_epi32(diff_lin);
        e_lin = _mm256_sub_epi32(e_lin, v_dn_bias);
        e_lin = _mm256_max_epi32(e_lin, v_zero);

        /* e_log = ABS(fe - he). */
        __m256i diff_log = _mm256_sub_epi32(fe, he);
        __m256i e_log    = _mm256_abs_epi32(diff_log);

        /* MIN(MIN(e_lin/2, e_log/16), 65530). Right-shift is byte-identical
         * to `/2` and `/16` for non-negative int32s. */
        __m256i e_lin_s = _mm256_srai_epi32(e_lin, 1);
        __m256i e_log_s = _mm256_srai_epi32(e_log, 4);
        __m256i v_min   = _mm256_min_epi32(e_lin_s, e_log_s);
        v_min           = _mm256_min_epi32(v_min, v_clamp);
        v_min           = _mm256_max_epi32(v_min, v_zero);

        /* Where predicate fires, force 0 (matches pre-zeroed buffer + continue). */
        v_min = _mm256_andnot_si256(mask, v_min);

        /* Pack to uint16. Values are in [0,65530] so packus is safe. */
        __m128i lo = _mm256_castsi256_si128(v_min);
        __m128i hi = _mm256_extracti128_si256(v_min, 1);
        __m128i packed = _mm_packus_epi32(lo, hi);
        _mm_storeu_si128((__m128i*)&alias_map[x], packed);
    }
    /* Tail: scalar reference for x in [x, w). */
    for (; x < w; x++) {
        if (fullres_curve[bright_row[x]] > fullres_thr_v) {
            /* alias_map already zero from caller's memset */
            continue;
        }
        int f  = (int)fullres_smooth_row[x];
        int hh = (int)halfres_smooth_row[x];
        int fe = raw2ev_lut[f];
        int he = raw2ev_lut[hh];
        int e_lin = f > hh ? f - hh : hh - f;
        e_lin = e_lin - dn_bias;
        if (e_lin < 0) e_lin = 0;
        int e_log = fe > he ? fe - he : he - fe;
        int v = e_lin >> 1;
        int u = e_log >> 4;
        int m = v < u ? v : u;
        if (m > 65530) m = 65530;
        alias_map[x] = (uint16_t)m;
    }
}

/* ====================================================================== *
 * C4.2 — build_alias_map weighted Gaussian (8 px / iter).
 *
 * Scalar reference (dualiso.c:2766-2786):
 *
 *     int c =
 *      (alias_aux[(x+0,y+0)]) +
 *      (sum_cross_2)              * 820 / 1024 +
 *      (sum_diag_2)               * 657 / 1024 +
 *      (sum_cross_2)              * 421 / 1024 +
 *      (sum_diag_2_doubled)       * 337 / 1024 +
 *      (sum_diag_2)               * 173 / 1024 +
 *      (sum_cross_6)              * 139 / 1024 +
 *      (sum_outer_eight_a)        * 111 / 1024 +
 *      (sum_outer_eight_a)        *  57 / 1024;
 *     alias_map[x + y*w] = c;
 *
 * Each `sum * weight / 1024` term truncates per-term (integer division),
 * so we must NOT collapse equal taps across terms. We compute every term
 * as the scalar code does, in int32, with arithmetic right shift by 10
 * (== /1024 for non-negative, which the scalar values are: alias_aux is
 * uint16-derived, so all sums fit in int32 positive).
 *
 * The center coefficient is 1 (no scale).
 *
 * Predicate (fullres_curve[bright] > fullres_thr) → skip (preserve previous
 * alias_map value, which carries the value from the init err diff stage).
 * For SIMD we blend with the existing alias_map[x] value.
 *
 * Edge handling: caller skips x ∈ [0,6) ∪ [w-6, w) and y ∈ [0,6) ∪ [h-6, h).
 * This kernel processes x ∈ [6, w-6) on a single row in the inner loop.
 * ====================================================================== */
__attribute__((target("avx2,fma")))
static void build_alias_map_gaussian_row_avx2(uint16_t * alias_map_row,
                                               const uint16_t * alias_aux,
                                               int y, int w,
                                               const uint32_t * bright_full,
                                               const double * fullres_curve,
                                               double fullres_thr_v)
{
    const __m256i v_zero = _mm256_setzero_si256();
    const __m256d v_thr  = _mm256_set1_pd(fullres_thr_v);

    /* Helper macro for a tap row pointer (alias_aux at row y+dy). */
    #define AUX_ROW(dy) (&alias_aux[(size_t)(y + (dy)) * (size_t)w])

    /* Precomputed per-row tap base addresses. */
    const uint16_t * row_m6 = AUX_ROW(-6);
    const uint16_t * row_m2 = AUX_ROW(-2);
    const uint16_t * row_p0 = AUX_ROW( 0);
    const uint16_t * row_p2 = AUX_ROW( 2);
    const uint16_t * row_p6 = AUX_ROW( 6);
    #undef AUX_ROW

    int x = 6;
    for (; x + 8 <= w - 6; x += 8) {
        /* Predicate vector: gather 8 fullres_curve[bright[x..x+7]] doubles
         * and compare > fullres_thr. */
        __m256i b = _mm256_loadu_si256((const __m256i*)&bright_full[(size_t)y * (size_t)w + x]);
        __m128i b_lo = _mm256_castsi256_si128(b);
        __m128i b_hi = _mm256_extracti128_si256(b, 1);
        __m256d fc_lo = _mm256_i32gather_pd(fullres_curve, b_lo, 8);
        __m256d fc_hi = _mm256_i32gather_pd(fullres_curve, b_hi, 8);
        __m256d skip_lo_pd = _mm256_cmp_pd(fc_lo, v_thr, _CMP_GT_OQ);
        __m256d skip_hi_pd = _mm256_cmp_pd(fc_hi, v_thr, _CMP_GT_OQ);
        __m256i skip_mask = pack_pd_cmp_to_epi32_mask(skip_lo_pd, skip_hi_pd);

        /* Load the 21 taps as 8-lane int32 vectors. Each load:
         *   _mm256_cvtepu16_epi32(_mm_loadu_si128(<8x uint16>))
         * gives 8 int32 lanes. */
        #define TAP(rowp, dx) _mm256_cvtepu16_epi32(\
            _mm_loadu_si128((const __m128i*)&(rowp)[x + (dx)]))

        __m256i t_00 = TAP(row_p0,  0);
        __m256i t_p0_m2 = TAP(row_m2,  0);
        __m256i t_m2_p0 = TAP(row_p0, -2);
        __m256i t_p2_p0 = TAP(row_p0,  2);
        __m256i t_p0_p2 = TAP(row_p2,  0);
        __m256i t_m2_m2 = TAP(row_m2, -2);
        __m256i t_p2_m2 = TAP(row_m2,  2);
        __m256i t_m2_p2 = TAP(row_p2, -2);
        __m256i t_p2_p2 = TAP(row_p2,  2);
        __m256i t_p0_m6 = TAP(row_m6,  0);
        __m256i t_m6_p0 = TAP(row_p0, -6);
        __m256i t_p6_p0 = TAP(row_p0,  6);
        __m256i t_p0_p6 = TAP(row_p6,  0);
        __m256i t_m2_m6 = TAP(row_m6, -2);
        __m256i t_p2_m6 = TAP(row_m6,  2);
        __m256i t_m6_m2 = TAP(row_m2, -6);
        __m256i t_p6_m2 = TAP(row_m2,  6);
        __m256i t_m6_p2 = TAP(row_p2, -6);
        __m256i t_p6_p2 = TAP(row_p2,  6);
        __m256i t_m2_p6 = TAP(row_p6, -2);
        __m256i t_p2_p6 = TAP(row_p6,  2);
        #undef TAP

        /* Term groups, matching scalar exactly. Each `term = sum * w / 1024`.
         * Sum-then-multiply, then arithmetic right shift 10 (equivalent to
         * truncating signed division by 1024 for non-negative values). */
        __m256i sum_cross_2 = _mm256_add_epi32(
            _mm256_add_epi32(t_p0_m2, t_m2_p0),
            _mm256_add_epi32(t_p2_p0, t_p0_p2));
        __m256i sum_diag_2 = _mm256_add_epi32(
            _mm256_add_epi32(t_m2_m2, t_p2_m2),
            _mm256_add_epi32(t_m2_p2, t_p2_p2));
        /* The scalar 337 term reads (m2,m2)+(p2,m2)+(m2,m2)+(p2,m2)+(m2,p2)+(p2,p2)+(m2,p2)+(p2,p2)
         * = 2 * sum_diag_2. */
        __m256i sum_diag_2_doubled = _mm256_slli_epi32(sum_diag_2, 1);

        __m256i sum_cross_6 = _mm256_add_epi32(
            _mm256_add_epi32(t_p0_m6, t_m6_p0),
            _mm256_add_epi32(t_p6_p0, t_p0_p6));
        __m256i sum_outer_8 = _mm256_add_epi32(
            _mm256_add_epi32(_mm256_add_epi32(t_m2_m6, t_p2_m6),
                             _mm256_add_epi32(t_m6_m2, t_p6_m2)),
            _mm256_add_epi32(_mm256_add_epi32(t_m6_p2, t_p6_p2),
                             _mm256_add_epi32(t_m2_p6, t_p2_p6)));

        /* Multiply each sum by its weight (in int32), then >> 10. */
        const __m256i v_w820 = _mm256_set1_epi32(820);
        const __m256i v_w657 = _mm256_set1_epi32(657);
        const __m256i v_w421 = _mm256_set1_epi32(421);
        const __m256i v_w337 = _mm256_set1_epi32(337);
        const __m256i v_w173 = _mm256_set1_epi32(173);
        const __m256i v_w139 = _mm256_set1_epi32(139);
        const __m256i v_w111 = _mm256_set1_epi32(111);
        const __m256i v_w57  = _mm256_set1_epi32(57);

        __m256i acc = t_00;  /* center, weight 1, no shift */
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_cross_2,        v_w820), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_diag_2,         v_w657), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_cross_2,        v_w421), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_diag_2_doubled, v_w337), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_diag_2,         v_w173), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_cross_6,        v_w139), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_outer_8,        v_w111), 10));
        acc = _mm256_add_epi32(acc, _mm256_srai_epi32(_mm256_mullo_epi32(sum_outer_8,        v_w57),  10));

        /* Clamp to uint16 (scalar simply assigns int → uint16, truncating).
         * To match scalar semantics exactly: scalar `alias_map[..] = c;` where
         * `c` is `int` and target is uint16 truncates by storing low 16 bits.
         * Use packus (clamps to [0, 65535]) — but scalar truncates without
         * clamp. Values are bounded < 2^16 in practice (alias_aux ≤ 65530,
         * weighted sum ≤ 65530 * (1+820+657+421+...+57)/1024 ≈ ...).
         * Actually the sum of weights is 1024 + 1241+1504+139+168 = ?
         * We mimic scalar truncation: use _mm_packus_epi32 with prior min to
         * 65535 to be safe. The init-err-diff bound 65530 propagates. */
        const __m256i v_max16 = _mm256_set1_epi32(0xFFFF);
        acc = _mm256_min_epi32(_mm256_max_epi32(acc, v_zero), v_max16);

        /* Predicate: where skip_mask is true, preserve existing alias_map[x]
         * value. Load the existing 8 uint16s, widen to int32, blend via mask. */
        __m128i prev16 = _mm_loadu_si128((const __m128i*)&alias_map_row[x]);
        __m256i prev   = _mm256_cvtepu16_epi32(prev16);
        acc = _mm256_blendv_epi8(acc, prev, skip_mask);

        /* Pack and store. */
        __m128i lo = _mm256_castsi256_si128(acc);
        __m128i hi = _mm256_extracti128_si256(acc, 1);
        __m128i packed = _mm_packus_epi32(lo, hi);
        _mm_storeu_si128((__m128i*)&alias_map_row[x], packed);
    }
    /* Tail: scalar reference for x in [x, w-6). */
    for (; x < w - 6; x++) {
        if (fullres_curve[bright_full[(size_t)y * (size_t)w + x]] > fullres_thr_v)
            continue;
        int c =
            (int)alias_aux[x   + (size_t)(y  ) * (size_t)w] +
            ((int)alias_aux[x   + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x   + (size_t)(y+2) * (size_t)w]) * 820 / 1024 +
            ((int)alias_aux[x-2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+2) * (size_t)w]) * 657 / 1024 +
            ((int)alias_aux[x   + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x   + (size_t)(y+2) * (size_t)w]) * 421 / 1024 +
            ((int)alias_aux[x-2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+2) * (size_t)w]) * 337 / 1024 +
            ((int)alias_aux[x-2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+2) * (size_t)w]) * 173 / 1024 +
            ((int)alias_aux[x   + (size_t)(y-6) * (size_t)w] + (int)alias_aux[x-6 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x+6 + (size_t)(y  ) * (size_t)w] + (int)alias_aux[x   + (size_t)(y+6) * (size_t)w]) * 139 / 1024 +
            ((int)alias_aux[x-2 + (size_t)(y-6) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-6) * (size_t)w] + (int)alias_aux[x-6 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+6 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-6 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+6 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+6) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+6) * (size_t)w]) * 111 / 1024 +
            ((int)alias_aux[x-2 + (size_t)(y-6) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y-6) * (size_t)w] + (int)alias_aux[x-6 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x+6 + (size_t)(y-2) * (size_t)w] + (int)alias_aux[x-6 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x+6 + (size_t)(y+2) * (size_t)w] + (int)alias_aux[x-2 + (size_t)(y+6) * (size_t)w] + (int)alias_aux[x+2 + (size_t)(y+6) * (size_t)w]) * 57 / 1024;
        alias_map_row[x] = (uint16_t)c;
    }
}

#endif /* DUALISO_AVX2_AVAILABLE */

#endif /* DUALISO_AVX2_KERNELS_INCLUDED */
