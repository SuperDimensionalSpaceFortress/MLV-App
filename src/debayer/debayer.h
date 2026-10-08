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
