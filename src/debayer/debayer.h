#ifndef _debayer_
#define _debayer_

#include <stdint.h>

/* Easy debayer types */
void debayerEasy(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads, int type);
/* Fast none-debayer path for already-processed uint16 raw frames. */
void debayerNoneU16(uint16_t * __restrict debayerto,
                    const uint16_t * __restrict bayerdata,
                    int width,
                    int height,
                    int threads,
                    int bit_shift);
void debayerBasicU16(uint16_t * __restrict debayerto,
                     uint16_t * __restrict bayerdata,
                     int width,
                     int height,
                     int threads,
                     int bit_shift);
/* Returns non-zero if debayerBasicU16 has latched the AVX2 fast path
 * (CPU supports avx2+fma, MLVAPP_DISABLE_AVX2 / MLVAPP_DISABLE_AVX2_DEBAYER
 * not set). Latches via pthread_once on first call. */
int debayerBasicU16Avx2Active(void);
/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: [1,2,1]/4 along x per channel of interleaved
 * RGB16, mirrored edge columns; the reduced texture route's horizontal Nyquist zero.
 * out and in must not alias. */
void debayer_reduced_hnyquist121_rgb16(uint16_t *out, const uint16_t *in, int w, int h);
/* CPU-DUALISO-REDUCED-RECON-H2C-PORT-1: the chroma-only twin. G passes through;
 * R' = G + h121(R - G), B' = G + h121(B - G) along x with h121 = (l + 2c + r) / 4,
 * the same mirrored edge columns, signed arithmetic rounded as the Nyquist zero
 * rounds ((s + 2) >> 2, floor), clamped to [0, 65535]. Rows are independent
 * (threads > 1: OpenMP over rows). out and in must not alias. */
void debayer_reduced_hchroma121_rgb16(uint16_t *out, const uint16_t *in, int w, int h, int threads);
/* r12: the same filter, then each R' and B' clamped to [min, max] of that channel in the
 * three input pixels it read (xl, x, xr). A channel constant over the three passes
 * through exactly whatever G does, so the x4 route's 2x2 box (a mean of in-range values)
 * cannot put a G-only edge back on it. G passes through. Same threading and aliasing
 * rules. The x4 CPU reduced route runs this one; the plain filter is r8's composition. */
void debayer_reduced_hchroma121_ranged_rgb16(uint16_t *out, const uint16_t *in, int w, int h, int threads);
/* CPU-DUALISO-REDUCED-RECON-H2C-PORT-1 r10: the x4 route's edge chroma. rgb holds
 * debayerBasicU16's output for bayer (RGGB, R at (even row, even col), bayer already
 * bit-shifted by that call). G is left as it is. R and B are rebuilt from a
 * gradient-directed Hamilton-Adams G (each direction clamped to its two G neighbours,
 * r11) and a bilinear colour-difference interpolation,
 * both in a balanced domain where R ~ aR * G + cR and B ~ aB * G + cB (a least-squares
 * fit per frame against the 4 G neighbours of every second R or B row; r11: a degenerate
 * fit leaves that channel exactly as debayerBasicU16 wrote it), and the colour difference
 * is laid on the bilinear G:
 * R = aR * (G + (R' - Gha)) + cR, rounded and clamped to [0, 65535], then (r11) to
 * [min, max] of that channel's own Bayer samples in the pixel's 5x5 window. Border sites
 * mirror (x - 1 -> x + 1, x - 2 -> x + 2). scratch holds w * h floats.
 * Returns 1 when R and B were rebuilt, 0 (rgb untouched) when w or h < 4 or a
 * pointer is NULL. Deterministic for any thread count (OpenMP over rows; the fit is
 * serial). */
int debayer_reduced_hacd_chroma_rgb16(uint16_t *rgb, const uint16_t *bayer, int w, int h, int threads,
                                      float *scratch);
/* r12 (adversary r12 pass 2 A1): in both steps above, a pixel whose own channel is locally
 * smoother than the balanced colour difference (curvature at its nearest own site, scaled
 * by the fit slope) takes that channel's own interpolation instead: in the HA-CD step the
 * bilinear mean of its nearest own samples, before the 5x5 clamp; in the chroma step the
 * plain [1 2 1] of the channel. An R or B that does not follow G (constant or affine under
 * a G-only edge) is then exact, while one that follows G keeps the colour difference.
 * The x4 CPU reduced route's chroma: debayer_reduced_hacd_chroma_rgb16 on rgb, then
 * debayer_reduced_hchroma121_ranged_rgb16 into filtered with that choice (the fit's
 * slopes; a kept channel, whose fit was degenerate, always takes its own plain [1 2 1],
 * r12 pass 3). Returns 0 with
 * filtered untouched when the HA-CD step refuses. */
int debayer_reduced_hacd_chroma121_rgb16(uint16_t *filtered, uint16_t *rgb, const uint16_t *bayer, int w, int h,
                                         int threads, float *scratch);
/* Quite quick bilinear debayer, floating point sadly; threads argument is unused */
void debayerBasic(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads);
/* More useable amaze, threads number should be the number of cores(or threads if >= i7) your cpu has */
int debayerAmaze(uint16_t * __restrict debayerto, float * __restrict bayerdata, int width, int height, int threads, int blacklevel);
void debayerAmazeSetThreadCreateFailureForTesting(int thread_index);
/* via librtprocess */
void debayerLibRtProcess(uint16_t *__restrict debayerto, float *__restrict bayerdata, int width, int height, int algorithm, double camMatrix[9]);
/* AHD debayer */
void debayerAhd(uint16_t *__restrict debayerto, float *__restrict bayerdata, int width, int height);

/* None debayer structure for multithread */
typedef struct {
    uint16_t * __restrict debayerto; //output
    float * __restrict bayerdata; //input
    int width; //complete width
    int height; //cropped height to render
    int offsetY; //start of crop for thread in height
} easydebayerinfo_t;

/* AMaZe input as struct for posixz */
typedef struct {
    float ** __restrict rawData;    /* holds preprocessed pixel values, rawData[i][j] corresponds to the ith row and jth column */
    float ** __restrict red;        /* the interpolated red plane */
    float ** __restrict green;      /* the interpolated green plane */
    float ** __restrict blue;       /* the interpolated blue plane */
    int winx; int winy; /* crop window for demosaicing */
    int winw; int winh;
    int cfa;
    int blacklevel;
} amazeinfo_t;

/* Amaze demosaic */
int
#ifdef __MINGW32__
/* Needed for win32/mingw (might need include gaurd with another compiler) */
__attribute__ ((force_align_arg_pointer))
#endif

/*AMaZE algo*/
demosaic(amazeinfo_t * inputdata);

/* Test-only fault injection for the AMaZE worker's private tile allocation.
 * Production leaves this disabled. */
void amazeDemosaicSetAllocationFailureForTesting(int enabled);

/* Test-only fault injection for debayerAmaze's wrapper allocations. */
void debayerAmazeSetAllocationFailureForTesting(int enabled);

/* Test-only ordinal fault injection for multithread helper allocations. */
void debayerAmazeSetAuxAllocationFailureForTesting(int allocation_index);

#endif
