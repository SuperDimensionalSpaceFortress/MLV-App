#ifndef _playback_downsample_
#define _playback_downsample_

#include <stdint.h>

/* Phase 4B: fused Bayer-to-RGB downsample-and-debayer.
 *
 * The pipeline still runs the full LJ92 decode + full llrawproc at native
 * resolution (these need the complete bitstream and Bayer pattern). After
 * llrawproc completes, the unpacked uint16 RGGB bayer image is fed into
 * pl_downsample_bayer_to_rgb_<2x|4x|8x>, which produces a half-/quarter-/
 * eighth-res RGB image directly via per-channel block averaging. The debayer
 * step is skipped entirely on the scaled path.
 *
 * Strategy: per 2x2 block of the source RGGB pattern, output one RGB pixel
 *   R := bayer[(0,0)]
 *   G := (bayer[(0,1)] + bayer[(1,0)]) / 2
 *   B := bayer[(1,1)]
 *
 * For scale=4, the 4x4 source region collapses to one RGB output pixel,
 * averaging 4 R taps, 8 G taps, and 4 B taps. For scale=8, the 8x8 source
 * region collapses to one RGB output pixel, averaging 16 R taps, 32 G taps,
 * and 16 B taps. The 4-row bayer-preserving pre-recon path remains scale=4;
 * scale=8 is the conservative post-llrawproc preview path.
 *
 * Output layout: AoS-3 (RGB interleaved per pixel), uint16 per channel.
 * The output bit depth is normalized to 16-bit; if the bayer input is in
 * 14-bit packed form the caller passes bit_shift=2 (or equivalent <<N).
 * Today video_mlv passes 0 because llrawproc has already widened to
 * 16-bit by the time we run.
 *
 * The 2x/4x kernels are AVX2-vectorised on x86_64 hosts; scalar fallback is
 * used otherwise. The 8x kernel is scalar and OMP-parallel by row because
 * the output is already small. Dispatch is pthread_once-latched. Kill switches:
 *   MLVAPP_DISABLE_AVX2                — disables every AVX2 kernel
 *   MLVAPP_DISABLE_AVX2_DOWNSAMPLE     — disables only this kernel
 */

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    PL_DOWNSAMPLE_NONE         = 1, /* scaleFactor=1, no-op (caller bypasses) */
    PL_DOWNSAMPLE_2x_BLOCK_AVG = 2, /* per-channel 2x2 block average */
    PL_DOWNSAMPLE_4x_BLOCK_AVG = 4, /* per-channel 4x4 block average (preserves 4-row dual ISO pattern) */
    PL_DOWNSAMPLE_8x_BLOCK_AVG = 8  /* per-channel 8x8 block average, post-llrawproc preview path */
} pl_downsample_strategy_t;

/* RGGB Bayer -> RGB16 via 2x2 block averaging.
 *
 *   bayer_in : full-resolution bayer at (in_w, in_h), one uint16 per pixel,
 *              row-major. RGGB layout assumed (R at (0,0), G at (0,1) and (1,0),
 *              B at (1,1)).
 *   in_w     : input width in pixels. Must be even. (in_w/2) is output width.
 *   in_h     : input height in pixels. Must be even. (in_h/2) is output height.
 *   rgb_out  : output RGB16, AoS-3 interleaved, sized 3 * (in_w/2) * (in_h/2).
 *   bit_shift: left-shift applied to each output sample after averaging,
 *              for callers that want to widen narrow-bitdepth bayer to
 *              16-bit (e.g. 0 for already-16-bit input, 2 for 14-bit, etc.).
 *   threads  : number of OMP threads. <=1 means serial. */
void pl_downsample_bayer_to_rgb_2x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads);

/* RGGB Bayer -> RGB16 via 4x4 block averaging.
 *
 * Same contract as the 2x kernel, but operates over 4x4 source regions
 * collapsing to one output pixel.
 *   in_w must be a multiple of 4. (in_w/4) is output width.
 *   in_h must be a multiple of 4. (in_h/4) is output height.
 *
 * Per output pixel:
 *   R := mean of {bayer[(0,0)], bayer[(0,2)], bayer[(2,0)], bayer[(2,2)]}
 *   G := mean of the 8 green positions in the 4x4 RGGB tile
 *   B := mean of {bayer[(1,1)], bayer[(1,3)], bayer[(3,1)], bayer[(3,3)]}
 */
void pl_downsample_bayer_to_rgb_4x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads);

/* RGGB Bayer -> RGB16 via 8x8 block averaging.
 *
 * Conservative preview-only path for explicit 1/8-resolution playback.
 * Operates after llrawproc/recon has produced a full-resolution bayer buffer.
 *   output width  = floor(in_w/8)
 *   output height = floor(in_h/8)
 *
 * Any trailing partial 8x8 blocks on the right or bottom edge are ignored.
 * This keeps common 4-aligned Magic Lantern frame sizes eligible for an x8
 * preview without changing full-resolution export or the x4 Dual ISO fast
 * path.
 *
 * Per output pixel:
 *   R := mean of the 16 red positions in the 8x8 RGGB tile
 *   G := mean of the 32 green positions in the 8x8 RGGB tile
 *   B := mean of the 16 blue positions in the 8x8 RGGB tile
 */
void pl_downsample_bayer_to_rgb_8x(const uint16_t * bayer_in,
                                   int in_w,
                                   int in_h,
                                   uint16_t * rgb_out,
                                   int bit_shift,
                                   int threads);

/* Phase 4B-v2: Bayer-preserving downsample kernels.
 *
 * Output is uint16 RGGB Bayer at reduced resolution. Designed to be applied
 * BEFORE HQ Dual ISO recon (and before the rest of llrawproc that operates
 * on the bayer image), so that the recon runs on a much smaller buffer.
 *
 * The 4-row dual-ISO bright/dark pattern is preserved by sampling complete
 * 4-row source blocks and then skipping forward by an integer multiple of 4
 * rows for the next output block. For scale=4: keep src rows 0-3, skip
 * 4-15, keep 16-19, ... so that out_y=0..3 mirror src_y=0..3 (the full
 * iso_patterns 4-row cycle), out_y=4..7 mirror src_y=16..19, and so on.
 *
 * The X axis is downsampled by averaging same-Bayer-position taps inside a
 * 4-col (or 2-col) tile, which preserves the RGGB pattern at the smaller
 * resolution.
 *
 * Both kernels output uint16 with no bit-shift (we run BEFORE llrawproc, so
 * the input is the raw 14-bit-stored-as-uint16 values from
 * getMlvRawFrameUint16, and the output is the same range). llrawproc's
 * make_14bit / undo_14bit logic still works because the value range is
 * preserved.
 *
 * pl_downsample_bayer_to_bayer_4x_block_stride16:
 *   Output dim: (in_w/4, in_h/4).
 *   Y stride: keep complete 4-row blocks, block-stride 16 in source space.
 *   X downsample: 2x2 same-Bayer-position averaging inside 4-col tiles.
 *   in_w must be a multiple of 4. in_h must be a multiple of 16.
 *   When in_h is NOT a multiple of 16, falls back to 8-row block stride
 *   (block-stride 8 → Y/2) which still preserves the 4-row pattern but
 *   scales Y by 2 instead of 4.
 *
 * pl_downsample_bayer_to_bayer_2x_block_stride8:
 *   Output dim: (in_w/2, in_h/2).
 *   Y stride: keep complete 4-row blocks, block-stride 8 in source space.
 *   X downsample: 2-tap same-Bayer-position averaging inside 4-col tiles.
 *   in_w must be a multiple of 4 so the output width stays Bayer-even.
 *   in_h must be a multiple of 8.
 *
 * pl_downsample_bayer_to_bayer_8x_block_stride32:
 *   Output dim: (in_w/8, in_h/8).
 *   Y stride: keep complete 4-row blocks, block-stride 32 in source space.
 *   X downsample: 4-tap same-Bayer-position averaging inside 8-col tiles.
 *   in_w must be a multiple of 16 so the output width stays Bayer-even.
 *   in_h must be a multiple of 32.
 *
 * All return 0 on success, non-zero if dimensions don't satisfy the
 * stride constraints (in which case the caller must fall back to the v1
 * post-llrawproc path). */
int pl_downsample_bayer_to_bayer_4x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads);

int pl_downsample_bayer_to_bayer_2x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads);

int pl_downsample_bayer_to_bayer_8x(const uint16_t * bayer_in,
                                    int in_w,
                                    int in_h,
                                    uint16_t * bayer_out,
                                    int * out_w,
                                    int * out_h,
                                    int threads);

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r4: phase-correct same-colour, same-ISO tent bin
 * for the GPU reduced recon. Same output dims and divisibility as the 2x/4x
 * kernels above (scale 2 or 4 only). Output (x, y) is centred on its own site,
 * t = S*o + (S-1)/2 on each axis. Columns take source cols s = x (mod 2) with
 * |s - t| < S, weight 2*(S - |s - t|); rows take source rows s = y (mod 4), so
 * the ISO-row class is kept, with |s - t| < 2S, weight 2*(2S - |s - t|). An
 * out-of-range tap reads the nearest in-range sample of its class. Integer, one
 * rounding: (acc + den/2) / den, den 512 at x4 and 32 at x2. Returns 0 on
 * success, non-zero on bad dims, scale or allocation failure. */
int pl_downsample_bayer_to_bayer_phase_tent(const uint16_t * in,
                                            int in_w,
                                            int in_h,
                                            uint16_t * out,
                                            int scale,
                                            int * out_w,
                                            int * out_h,
                                            int threads);

/* Phase 4B-v2 (X-only): bayer-to-bayer 4x reduction in X only, identity
 * in Y. Used when the source height is not 16-aligned and so the
 * Y-axis-also-downsample 4x kernel would reject. Output dim:
 * (in_w/4, in_h). Preserves RGGB pattern. The dual-ISO 4-row bright/dark
 * pattern is unchanged because Y is identity.
 *
 * Returns 0 on success, non-zero if dimensions don't satisfy constraints
 * (in_w must be multiple of 4). */
int pl_downsample_bayer_to_bayer_4x_x_only(const uint16_t * bayer_in,
                                            int in_w,
                                            int in_h,
                                            uint16_t * bayer_out,
                                            int * out_w,
                                            int * out_h,
                                            int threads);

/* Phase 4B-v2 (post-recon Y-only RGB downsample): RGB→RGB 4x reduction
 * in Y only, identity in X. Used after debayering the (W/4, H) recon
 * output to produce the (W/4, H/4) final RGB output.
 *
 *   in_rgb  : input RGB16 at (in_w, in_h), AoS-3 interleaved.
 *   in_h must be a multiple of 4.
 * Returns 0 on success. */
int pl_downsample_rgb_to_rgb_4x_y_only(const uint16_t * in_rgb,
                                       int in_w,
                                       int in_h,
                                       uint16_t * out_rgb,
                                       int * out_w,
                                       int * out_h,
                                       int threads);

/* Returns 1 if the AVX2 fast path is in use for the downsample kernels,
 * 0 otherwise. Reads kill-switch env vars on first call. */
int plDownsampleAvx2Active(void);

/* Test-only hook: re-evaluate dispatch from current env state. Used by
 * the parity tests that flip MLVAPP_DISABLE_AVX2_DOWNSAMPLE. */
int plDownsampleReinitDispatchForTesting(void);

#ifdef __cplusplus
}
#endif

#endif /* _playback_downsample_ */
