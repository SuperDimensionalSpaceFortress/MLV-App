/* dualiso_mix_chroma_avx2.inc - AVX2 kernel for the dual-ISO matched-source
 * 2x2 chroma smooth (CPU-S1-DUALISO-MIX-CHROMA-LEVER-2 K4).
 *
 * Included from dualiso.c after the K1/K2 scalar kernel (it reuses
 * dualiso_cs2_pos_row_t, chroma_smooth_med5 and dualiso_cs2_store_range) and
 * only when DUALISO_AVX2_AVAILABLE. Every function carries
 * target("avx2,fma"); dualiso_mix_chroma_dispatch_init decides at run time.
 *
 * Byte identity with the scalar K1/K2 kernel (and so with the legacy
 * chroma_smooth_2x2 uint32 instantiation) rests on:
 *  - (a) EV rows: each raw pixel of rows p-1..p+2 is converted ONCE through
 *    the same clamped raw2ev lookup as DUALISO_CS2_EV (index clamped to the
 *    LUT end, an unsigned min), into a per-thread ring of 4 EV rows stored
 *    as even and odd columns. A position then reads 8 EV values instead of
 *    making 8 lookups; each new position row converts 2 raw rows.
 *  - signed /2 and /4 round toward zero: (x + ((x>>31)&1))>>1 and
 *    (x + ((x>>31)&3))>>2 (srai alone rounds down);
 *  - (unsigned)r0 < white_u is an unsigned compare (both sides biased by
 *    0x80000000); r0 < black_thr is a signed compare;
 *  - use_average is a mask OR; choose is ev < eh; dr/db/gr/gb are blends;
 *  - the ev2raw index is clamped for every lane before the gather;
 *  - outputs are stride 2: the copied row is loaded, only written lanes are
 *    blended in, and the row is stored back;
 *  - the ncx % 8 tail runs the scalar store (dualiso_cs2_store_range).
 * Test-only mutation (g_dualiso_cs2_mutation_for_testing): 1 swaps k1/k3
 * and flips the V-blue sign (as the scalar kernel); 2 turns the truncating
 * /2 and /4 into plain srai (AVX2 only). Always 0 in the product. */

#define DUALISO_CS2_AVX2 __attribute__((target("avx2,fma")))

/* x / 2 rounding toward zero; bias1 is 1 (exact) or 0 (mutation 2: srai). */
DUALISO_CS2_AVX2
static inline __m256i dualiso_cs2_div2_avx2(__m256i x, __m256i bias1)
{
    return _mm256_srai_epi32(_mm256_add_epi32(x, _mm256_and_si256(_mm256_srai_epi32(x, 31), bias1)), 1);
}

/* x / 4 rounding toward zero; bias3 is 3 (exact) or 0 (mutation 2: srai). */
DUALISO_CS2_AVX2
static inline __m256i dualiso_cs2_div4_avx2(__m256i x, __m256i bias3)
{
    return _mm256_srai_epi32(_mm256_add_epi32(x, _mm256_and_si256(_mm256_srai_epi32(x, 31), bias3)), 2);
}

/* Even and odd 32-bit lanes of the 16 values at p. */
DUALISO_CS2_AVX2
static inline void dualiso_cs2_deinterleave_avx2(const void * p, __m256i * even, __m256i * odd)
{
    const __m256 a = _mm256_castsi256_ps(_mm256_loadu_si256((const __m256i *)p));
    const __m256 b = _mm256_castsi256_ps(_mm256_loadu_si256((const __m256i *)p + 1));
    *even = _mm256_permute4x64_epi64(_mm256_castps_si256(_mm256_shuffle_ps(a, b, _MM_SHUFFLE(2, 0, 2, 0))),
                                     _MM_SHUFFLE(3, 1, 2, 0));
    *odd = _mm256_permute4x64_epi64(_mm256_castps_si256(_mm256_shuffle_ps(a, b, _MM_SHUFFLE(3, 1, 3, 1))),
                                    _MM_SHUFFLE(3, 1, 2, 0));
}

/* One raw row to EV: ev_even[k] = EV(row[2k]), ev_odd[k] = EV(row[2k+1]) for
 * k < nk, with the DUALISO_CS2_EV clamp; columns past the row end read 0 and
 * are never used. */
DUALISO_CS2_AVX2
static void dualiso_cs2_ev_row_avx2(const uint32_t * __restrict row,
                                    int w,
                                    int nk,
                                    const int * __restrict raw2ev,
                                    int * __restrict ev_even,
                                    int * __restrict ev_odd)
{
    const __m256i lut_max = _mm256_set1_epi32((int)(DUALISO_RAW2EV_LUT_COUNT - 1u));
    int k = 0;
    for (; k + 8 <= nk && 2 * k + 16 <= w; k += 8)
    {
        __m256i e;
        __m256i o;
        dualiso_cs2_deinterleave_avx2(row + 2 * k, &e, &o);
        e = _mm256_min_epu32(e, lut_max);
        o = _mm256_min_epu32(o, lut_max);
        _mm256_storeu_si256((__m256i *)(ev_even + k), _mm256_i32gather_epi32(raw2ev, e, 4));
        _mm256_storeu_si256((__m256i *)(ev_odd + k), _mm256_i32gather_epi32(raw2ev, o, 4));
    }
    for (; k < nk; k++)
    {
        const int ce = 2 * k;
        ev_even[k] = ce < w ? raw2ev[MIN(row[ce], (uint32_t)(DUALISO_RAW2EV_LUT_COUNT - 1u))] : 0;
        ev_odd[k] = ce + 1 < w ? raw2ev[MIN(row[ce + 1], (uint32_t)(DUALISO_RAW2EV_LUT_COUNT - 1u))] : 0;
    }
}

/* Position row at p from EV rows p-1 (even cols), p, p+1 and p+2 (odd cols).
 * Position j (q = 2 + 2j): rr = E(p, q) = e0[1+j], g1 = E(p, q+1) = o0[1+j],
 * g3 = E(p, q-1) = o0[j], bb = E(p+1, q+1) = o1[1+j], g2 = E(p+1, q) =
 * e1[1+j], g5 = E(p+1, q+2) = e1[2+j], g4 = E(p-1, q) = em1[1+j],
 * g6 = E(p+2, q+1) = o2[1+j]. Same fields as dualiso_cs2_fill_pos_row. */
DUALISO_CS2_AVX2
static void dualiso_cs2_fill_pos_row_avx2(const int * __restrict em1,
                                          const int * __restrict e0,
                                          const int * __restrict o0,
                                          const int * __restrict e1,
                                          const int * __restrict o1,
                                          const int * __restrict o2,
                                          int npos,
                                          int mutation,
                                          const dualiso_cs2_pos_row_t * row)
{
    const __m256i bias1 = _mm256_set1_epi32(mutation == 2 ? 0 : 1);
    int j = 0;
    for (; j + 8 <= npos; j += 8)
    {
        const __m256i rr = _mm256_loadu_si256((const __m256i *)(e0 + 1 + j));
        const __m256i g1 = _mm256_loadu_si256((const __m256i *)(o0 + 1 + j));
        const __m256i g3 = _mm256_loadu_si256((const __m256i *)(o0 + j));
        const __m256i bb = _mm256_loadu_si256((const __m256i *)(o1 + 1 + j));
        const __m256i g2 = _mm256_loadu_si256((const __m256i *)(e1 + 1 + j));
        const __m256i g5 = _mm256_loadu_si256((const __m256i *)(e1 + 2 + j));
        const __m256i g4 = _mm256_loadu_si256((const __m256i *)(em1 + 1 + j));
        const __m256i g6 = _mm256_loadu_si256((const __m256i *)(o2 + 1 + j));
        const __m256i half16 = dualiso_cs2_div2_avx2(_mm256_add_epi32(g1, g6), bias1);
        _mm256_storeu_si256((__m256i *)(row->hr + j),
                            _mm256_sub_epi32(rr, dualiso_cs2_div2_avx2(_mm256_add_epi32(g1, g3), bias1)));
        _mm256_storeu_si256((__m256i *)(row->hb + j),
                            _mm256_sub_epi32(bb, dualiso_cs2_div2_avx2(_mm256_add_epi32(g2, g5), bias1)));
        _mm256_storeu_si256((__m256i *)(row->eht + j),
                            _mm256_add_epi32(_mm256_abs_epi32(_mm256_sub_epi32(g1, g3)),
                                             _mm256_abs_epi32(_mm256_sub_epi32(g2, g5))));
        _mm256_storeu_si256((__m256i *)(row->vr + j),
                            _mm256_sub_epi32(rr, dualiso_cs2_div2_avx2(_mm256_add_epi32(g2, g4), bias1)));
        _mm256_storeu_si256((__m256i *)(row->vb + j),
                            mutation == 1 ? _mm256_sub_epi32(half16, bb) : _mm256_sub_epi32(bb, half16));
        _mm256_storeu_si256((__m256i *)(row->evt + j),
                            _mm256_add_epi32(_mm256_abs_epi32(_mm256_sub_epi32(g2, g4)),
                                             _mm256_abs_epi32(_mm256_sub_epi32(g1, g6))));
        _mm256_storeu_si256((__m256i *)(row->g1 + j), g1);
        _mm256_storeu_si256((__m256i *)(row->g2 + j), g2);
        _mm256_storeu_si256((__m256i *)(row->g3 + j), g3);
        _mm256_storeu_si256((__m256i *)(row->g4 + j), g4);
        _mm256_storeu_si256((__m256i *)(row->g5 + j), g5);
        _mm256_storeu_si256((__m256i *)(row->g6 + j), g6);
    }
    for (; j < npos; j++)
    {
        const int rr = e0[1 + j];
        const int g1 = o0[1 + j];
        const int g3 = o0[j];
        const int bb = o1[1 + j];
        const int g2 = e1[1 + j];
        const int g5 = e1[2 + j];
        const int g4 = em1[1 + j];
        const int g6 = o2[1 + j];
        row->hr[j] = rr - (g1 + g3) / 2;
        row->hb[j] = bb - (g2 + g5) / 2;
        row->eht[j] = ABS(g1 - g3) + ABS(g2 - g5);
        row->vr[j] = rr - (g2 + g4) / 2;
        row->vb[j] = mutation == 1 ? (g1 + g6) / 2 - bb : bb - (g1 + g6) / 2;
        row->evt[j] = ABS(g2 - g4) + ABS(g1 - g6);
        row->g1[j] = g1;
        row->g2[j] = g2;
        row->g3[j] = g3;
        row->g4[j] = g4;
        row->g5[j] = g5;
        row->g6[j] = g6;
    }
}

/* chroma_smooth_med5's compare-exchange network, 8 lanes at a time. */
DUALISO_CS2_AVX2
static inline __m256i dualiso_cs2_med5_avx2(__m256i a0, __m256i a1, __m256i a2, __m256i a3, __m256i a4)
{
    #define CS2_VSWAP(lo, hi) do { \
        const __m256i _cs2_lo = _mm256_min_epi32((lo), (hi)); \
        const __m256i _cs2_hi = _mm256_max_epi32((lo), (hi)); \
        (lo) = _cs2_lo; \
        (hi) = _cs2_hi; \
    } while (0)
    CS2_VSWAP(a0, a1);
    CS2_VSWAP(a3, a4);
    CS2_VSWAP(a0, a3);
    CS2_VSWAP(a1, a4);
    CS2_VSWAP(a1, a2);
    CS2_VSWAP(a2, a3);
    CS2_VSWAP(a1, a2);
    #undef CS2_VSWAP
    return a2;
}

/* Same k order as dualiso_cs2_med5_pass / dualiso_cs2_sum5_pass. */
DUALISO_CS2_AVX2
static void dualiso_cs2_cross_pass_avx2(int ncx,
                                        const int * __restrict up,
                                        const int * __restrict mid,
                                        const int * __restrict dn,
                                        int * __restrict out,
                                        int is_sum)
{
    int i = 0;
    for (; i + 8 <= ncx; i += 8)
    {
        const __m256i k0 = _mm256_loadu_si256((const __m256i *)(mid + i));
        const __m256i k1 = _mm256_loadu_si256((const __m256i *)(up + i + 1));
        const __m256i k2 = _mm256_loadu_si256((const __m256i *)(mid + i + 1));
        const __m256i k3 = _mm256_loadu_si256((const __m256i *)(dn + i + 1));
        const __m256i k4 = _mm256_loadu_si256((const __m256i *)(mid + i + 2));
        const __m256i r = is_sum
            ? _mm256_add_epi32(_mm256_add_epi32(_mm256_add_epi32(_mm256_add_epi32(k0, k1), k2), k3), k4)
            : dualiso_cs2_med5_avx2(k0, k1, k2, k3, k4);
        _mm256_storeu_si256((__m256i *)(out + i), r);
    }
    for (; i < ncx; i++)
    {
        out[i] = is_sum
            ? mid[i] + up[i + 1] + mid[i + 1] + dn[i + 1] + mid[i + 2]
            : chroma_smooth_med5(mid[i], up[i + 1], mid[i + 1], dn[i + 1], mid[i + 2]);
    }
}

/* AVX2 form of dualiso_cs2_cell_row: 8 cells per step, scalar tail.
 * Returns the number of 8-cell steps whose vector store ran (a step whose
 * cells are all clipped is skipped and not counted; adversary a16ec254 A1). */
DUALISO_CS2_AVX2
static int dualiso_cs2_cell_row_avx2(const uint32_t * __restrict inp,
                                      uint32_t * __restrict out,
                                      int w,
                                      int y,
                                      int ncx,
                                      const dualiso_cs2_pos_row_t * up,
                                      const dualiso_cs2_pos_row_t * mid,
                                      const dualiso_cs2_pos_row_t * dn,
                                      int * __restrict cell,
                                      const int * __restrict ev2raw,
                                      int black,
                                      int white,
                                      int mutation)
{
    const uint32_t * row_y = inp + (size_t)y * (size_t)w;
    const uint32_t * row_y_p1 = row_y + w;
    uint32_t * out_y = out + (size_t)y * (size_t)w;
    uint32_t * out_y_p1 = out_y + w;
    const size_t n = (size_t)ncx;
    int * c_drh = cell;
    int * c_dbh = cell + n;
    int * c_drv = cell + 2 * n;
    int * c_dbv = cell + 3 * n;
    int * c_eh = cell + 4 * n;
    int * c_ev = cell + 5 * n;

    dualiso_cs2_cross_pass_avx2(ncx, up->hr, mid->hr, dn->hr, c_drh, 0);
    dualiso_cs2_cross_pass_avx2(ncx, up->hb, mid->hb, dn->hb, c_dbh, 0);
    dualiso_cs2_cross_pass_avx2(ncx, up->vr, mid->vr, dn->vr, c_drv, 0);
    dualiso_cs2_cross_pass_avx2(ncx, up->vb, mid->vb, dn->vb, c_dbv, 0);
    dualiso_cs2_cross_pass_avx2(ncx, up->eht, mid->eht, dn->eht, c_eh, 1);
    dualiso_cs2_cross_pass_avx2(ncx, up->evt, mid->evt, dn->evt, c_ev, 1);

    /* K2: whole output rows y and y+1 start as the input. */
    memcpy(out_y, row_y, (size_t)2 * (size_t)w * sizeof(uint32_t));

    const __m256i sign = _mm256_set1_epi32((int)0x80000000u);
    const __m256i white_biased = _mm256_xor_si256(_mm256_set1_epi32(white), sign);
    /* Same margin as the uint32 instantiation's CHROMA_SMOOTH_RAW_BLACK_MARGIN. */
    const __m256i black_thr = _mm256_set1_epi32(black + 64 * 64);
    const __m256i dir_thr = _mm256_set1_epi32(64);
    const __m256i ev_lo = _mm256_set1_epi32(-10 * EV_RESOLUTION);
    const __m256i ev_hi = _mm256_set1_epi32(14 * EV_RESOLUTION - 1);
    const __m256i bias1 = _mm256_set1_epi32(mutation == 2 ? 0 : 1);
    const __m256i bias3 = _mm256_set1_epi32(mutation == 2 ? 0 : 3);
    const __m256i zero = _mm256_setzero_si256();

    int vector_stores = 0;
    int i = 0;
    for (; i + 8 <= ncx; i += 8)
    {
        const int x0 = 4 + 2 * i;
        __m256i r0;
        __m256i r_odd;
        __m256i b_even;
        __m256i b0;
        dualiso_cs2_deinterleave_avx2(row_y + x0, &r0, &r_odd);
        dualiso_cs2_deinterleave_avx2(row_y_p1 + x0, &b_even, &b0);
        (void)r_odd;
        (void)b_even;
        /* (unsigned)v < white_u, as a signed compare of biased values. */
        const __m256i write_r = _mm256_cmpgt_epi32(white_biased, _mm256_xor_si256(r0, sign));
        const __m256i write_b = _mm256_cmpgt_epi32(white_biased, _mm256_xor_si256(b0, sign));
        const __m256i any = _mm256_or_si256(write_r, write_b);
        if (_mm256_testz_si256(any, any))
        {
            continue;
        }
        const __m256i drh = _mm256_loadu_si256((const __m256i *)(c_drh + i));
        const __m256i dbh = _mm256_loadu_si256((const __m256i *)(c_dbh + i));
        const __m256i drv = _mm256_loadu_si256((const __m256i *)(c_drv + i));
        const __m256i dbv = _mm256_loadu_si256((const __m256i *)(c_dbv + i));
        const __m256i eh = _mm256_loadu_si256((const __m256i *)(c_eh + i));
        const __m256i ev = _mm256_loadu_si256((const __m256i *)(c_ev + i));
        /* Centre g1..g6 are the k2 position's own gathers. */
        const __m256i g1 = _mm256_loadu_si256((const __m256i *)(mid->g1 + 1 + i));
        const __m256i g2 = _mm256_loadu_si256((const __m256i *)(mid->g2 + 1 + i));
        const __m256i g3 = _mm256_loadu_si256((const __m256i *)(mid->g3 + 1 + i));
        const __m256i g4 = _mm256_loadu_si256((const __m256i *)(mid->g4 + 1 + i));
        const __m256i g5 = _mm256_loadu_si256((const __m256i *)(mid->g5 + 1 + i));
        const __m256i g6 = _mm256_loadu_si256((const __m256i *)(mid->g6 + 1 + i));
        const __m256i grv = dualiso_cs2_div2_avx2(_mm256_add_epi32(g2, g4), bias1);
        const __m256i grh = dualiso_cs2_div2_avx2(_mm256_add_epi32(g1, g3), bias1);
        const __m256i gbv = dualiso_cs2_div2_avx2(_mm256_add_epi32(g1, g6), bias1);
        const __m256i gbh = dualiso_cs2_div2_avx2(_mm256_add_epi32(g2, g5), bias1);
        /* choose = ev < eh */
        const __m256i choose = _mm256_cmpgt_epi32(eh, ev);
        __m256i use_average = _mm256_or_si256(_mm256_cmpgt_epi32(black_thr, r0),
                                              _mm256_cmpgt_epi32(black_thr, b0));
        use_average = _mm256_or_si256(use_average,
                                      _mm256_cmpgt_epi32(dir_thr, _mm256_abs_epi32(_mm256_sub_epi32(drv, drh))));
        use_average = _mm256_or_si256(use_average,
                                      _mm256_cmpgt_epi32(dir_thr, _mm256_abs_epi32(_mm256_sub_epi32(grv, grh))));
        use_average = _mm256_or_si256(use_average,
                                      _mm256_cmpgt_epi32(dir_thr, _mm256_abs_epi32(_mm256_sub_epi32(gbv, gbh))));
        const __m256i dr_avg = dualiso_cs2_div2_avx2(_mm256_add_epi32(drv, drh), bias1);
        const __m256i db_avg = dualiso_cs2_div2_avx2(_mm256_add_epi32(dbv, dbh), bias1);
        const __m256i gr_avg = dualiso_cs2_div4_avx2(
            _mm256_add_epi32(_mm256_add_epi32(_mm256_add_epi32(g1, g2), g3), g4), bias3);
        const __m256i gb_avg = dualiso_cs2_div4_avx2(
            _mm256_add_epi32(_mm256_add_epi32(_mm256_add_epi32(g1, g2), g5), g6), bias3);
        const __m256i dr = _mm256_blendv_epi8(_mm256_blendv_epi8(drh, drv, choose), dr_avg, use_average);
        const __m256i db = _mm256_blendv_epi8(_mm256_blendv_epi8(dbh, dbv, choose), db_avg, use_average);
        const __m256i gr = _mm256_blendv_epi8(_mm256_blendv_epi8(grh, grv, choose), gr_avg, use_average);
        const __m256i gb = _mm256_blendv_epi8(_mm256_blendv_epi8(gbh, gbv, choose), gb_avg, use_average);
        /* Clamp every lane (written or not) before the gather. */
        const __m256i er = _mm256_max_epi32(_mm256_min_epi32(_mm256_add_epi32(gr, dr), ev_hi), ev_lo);
        const __m256i eb = _mm256_max_epi32(_mm256_min_epi32(_mm256_add_epi32(gb, db), ev_hi), ev_lo);
        const __m256i vr = _mm256_permute4x64_epi64(_mm256_i32gather_epi32(ev2raw, er, 4), _MM_SHUFFLE(3, 1, 2, 0));
        const __m256i vb = _mm256_permute4x64_epi64(_mm256_i32gather_epi32(ev2raw, eb, 4), _MM_SHUFFLE(3, 1, 2, 0));
        const __m256i mr = _mm256_permute4x64_epi64(write_r, _MM_SHUFFLE(3, 1, 2, 0));
        const __m256i mb = _mm256_permute4x64_epi64(write_b, _MM_SHUFFLE(3, 1, 2, 0));
        /* R lands on even lanes of row y at x0.., B on odd lanes of row y+1. */
        __m256i * pr = (__m256i *)(out_y + x0);
        __m256i * pb = (__m256i *)(out_y_p1 + x0);
        _mm256_storeu_si256(pr, _mm256_blendv_epi8(_mm256_loadu_si256(pr),
                                                   _mm256_unpacklo_epi32(vr, vr),
                                                   _mm256_unpacklo_epi32(mr, zero)));
        _mm256_storeu_si256(pr + 1, _mm256_blendv_epi8(_mm256_loadu_si256(pr + 1),
                                                       _mm256_unpackhi_epi32(vr, vr),
                                                       _mm256_unpackhi_epi32(mr, zero)));
        _mm256_storeu_si256(pb, _mm256_blendv_epi8(_mm256_loadu_si256(pb),
                                                   _mm256_unpacklo_epi32(vb, vb),
                                                   _mm256_unpacklo_epi32(zero, mb)));
        _mm256_storeu_si256(pb + 1, _mm256_blendv_epi8(_mm256_loadu_si256(pb + 1),
                                                       _mm256_unpackhi_epi32(vb, vb),
                                                       _mm256_unpackhi_epi32(zero, mb)));
        vector_stores++;
    }
    dualiso_cs2_store_range(inp, out, w, y, ncx, i, ncx, mid, cell, ev2raw, black, white);
    return vector_stores;
}

/* One thread band of cell rows [i0, i1): position row pi (p = 2 + 2pi)
 * reads raw rows 2pi+1 .. 2pi+4, kept as EV rows in a ring of 4 indexed by
 * raw row & 3 (each holds nk = npos + 2 even then nk odd values).
 * Returns the band's count of vector stores (dualiso_cs2_cell_row_avx2). */
DUALISO_CS2_AVX2
static long long dualiso_cs2_band_avx2(const uint32_t * __restrict inp,
                                  uint32_t * __restrict out,
                                  int w,
                                  int i0,
                                  int i1,
                                  int ncx,
                                  int npos,
                                  const int * __restrict raw2ev,
                                  const int * __restrict ev2raw,
                                  int black,
                                  int white,
                                  int mutation,
                                  dualiso_cs2_pos_row_t * slot,
                                  int * cell,
                                  int * ev_ring)
{
    const int nk = npos + 2;
    #define CS2_EV_EVEN(r) (ev_ring + (size_t)((r) & 3) * (size_t)2 * (size_t)nk)
    #define CS2_EV_ODD(r) (CS2_EV_EVEN(r) + nk)
    #define CS2_EV_CONVERT(r) dualiso_cs2_ev_row_avx2(inp + (size_t)(r) * (size_t)w, w, nk, raw2ev, \
                                                      CS2_EV_EVEN(r), CS2_EV_ODD(r))
    #define CS2_FILL(pi) do { \
        const int _p = 2 + 2 * (pi); \
        dualiso_cs2_fill_pos_row_avx2(CS2_EV_EVEN(_p - 1), CS2_EV_EVEN(_p), CS2_EV_ODD(_p), \
                                      CS2_EV_EVEN(_p + 1), CS2_EV_ODD(_p + 1), CS2_EV_ODD(_p + 2), \
                                      npos, mutation, &slot[(pi) % 3]); \
    } while (0)

    for (int r = 2 * i0 + 1; r <= 2 * i0 + 4; r++)
    {
        CS2_EV_CONVERT(r);
    }
    CS2_FILL(i0);
    CS2_EV_CONVERT(2 * i0 + 5);
    CS2_EV_CONVERT(2 * i0 + 6);
    CS2_FILL(i0 + 1);
    long long vector_stores = 0;
    for (int i = i0; i < i1; i++)
    {
        CS2_EV_CONVERT(2 * i + 7);
        CS2_EV_CONVERT(2 * i + 8);
        CS2_FILL(i + 2);
        const dualiso_cs2_pos_row_t * up = &slot[i % 3];
        const dualiso_cs2_pos_row_t * dn = &slot[(i + 2) % 3];
        vector_stores += dualiso_cs2_cell_row_avx2(inp, out, w, 4 + 2 * i, ncx,
                                                   mutation == 1 ? dn : up, &slot[(i + 1) % 3],
                                                   mutation == 1 ? up : dn,
                                                   cell, ev2raw, black, white, mutation);
    }
    #undef CS2_FILL
    #undef CS2_EV_CONVERT
    #undef CS2_EV_ODD
    #undef CS2_EV_EVEN
    return vector_stores;
}
