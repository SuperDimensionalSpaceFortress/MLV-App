/* cuda_amaze_debayer_stage_probe.cu - first CUDA stage probe for the generic
 * AMaZE debayer path (src/debayer/amaze_demosaic.c).
 *
 * This is deliberately NOT the production GPU AMaZE backend and does not make
 * GPU AMaZE available in MLVApp. It ports the first deterministic AMaZE tile
 * stages to CUDA and compares them against a host oracle:
 *
 *   - 16-pixel reflected tile halo load + CFA normalization
 *   - green-site seed plane
 *   - horizontal/vertical gradient weights
 *   - diagonal precursor weights used by later R/B interpolation
 *   - first green-direction interpolation color-difference planes
 *   - scalar-order variance selection and saturation-bound refinement
 *   - adaptive horizontal/vertical green interpolation weights (hvwt)
 *   - Nyquist texture detection flags
 *   - Nyquist-region area interpolation hvwt refinement
 *   - green-plane assembly at red/blue sites
 *   - Nyquist-region green-plane refinement
 *   - diagonal red/blue interpolation candidates, pmwt refinement, and rbint
 *   - diagonal green correction at red/blue sites
 *   - G-R/G-B coset split and fancy chrominance interpolation
 *   - final red/green/blue output-plane assembly
 *
 * The purpose is to land a small, reviewable, real-device AMaZE slice before
 * the full demosaic port. Full P-pre remains blocked until the final GPU AMaZE
 * RGB16 output compares against CPU AMaZE on real hardware.
 *
 * Build/run on Ultra-Magnus:
 *   pwsh -File amaze-debayer-stage-build-run.ps1
 */

#include <cuda_runtime.h>

/* GPU-EVENT153-ROOTCAUSE-2: the launch helpers below are compiled into the
 * AMaZE backend DLL, which times every launch (env-gated, default OFF). */
#include "backend/igpu_launch_timing.cuh"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <stdexcept>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

namespace
{
constexpr int kTileSize = 512;
constexpr int kTileHalf = kTileSize / 2;
constexpr int kTileSamples = kTileSize * kTileSize;
constexpr int kHalfTileSamples = kTileSize * kTileHalf;
constexpr float kEps = 1.0e-5f;
constexpr float kAdaptiveRatioThreshold = 0.75f;
constexpr float kClipPoint = 1.0f;
constexpr float kClipPoint8 = 0.8f;
constexpr float kNyquistThreshold = 0.5f;
constexpr float kTolerance = 1.0e-6f;
constexpr int kVarianceWavefrontThreads = 256;
constexpr int kVariancePrefixThreads = 256;
constexpr int kNyquistPrefixThreads = 256;
constexpr int kGreenPlaneThreads = 256;
constexpr int kPmwtRowThreads = 256;

struct CaseSpec
{
    const char * name;
    int width;
    int height;
    int top;
    int left;
};

struct StageBuffers
{
    std::vector<float> cfa;
    std::vector<float> rgbgreen;
    std::vector<float> red;
    std::vector<float> green;
    std::vector<float> blue;
    std::vector<float> dirwts0;
    std::vector<float> dirwts1;
    std::vector<float> delhvsqsum;
    std::vector<float> delp;
    std::vector<float> delm;
    std::vector<float> dgrbsq1p;
    std::vector<float> dgrbsq1m;
    std::vector<float> vcd;
    std::vector<float> hcd;
    std::vector<float> vcdalt;
    std::vector<float> hcdalt;
    std::vector<float> dgintv;
    std::vector<float> dginth;
    std::vector<float> cddiffsq;
    std::vector<float> hvwt;
    std::vector<unsigned char> nyquist;
    std::vector<float> dgrb0;
    std::vector<float> dgrb1;
    std::vector<float> dgrb2h;
    std::vector<float> dgrb2v;
    std::vector<float> rbm;
    std::vector<float> rbp;
    std::vector<float> pmwt;
    std::vector<float> pmwtalt;
    std::vector<float> rbint;

    StageBuffers()
        : cfa(kTileSamples, 0.0f)
        , rgbgreen(kTileSamples, 0.0f)
        , red(kTileSamples, 0.0f)
        , green(kTileSamples, 0.0f)
        , blue(kTileSamples, 0.0f)
        , dirwts0(kTileSamples, 0.0f)
        , dirwts1(kTileSamples, 0.0f)
        , delhvsqsum(kTileSamples, 0.0f)
        , delp(kHalfTileSamples, 0.0f)
        , delm(kHalfTileSamples, 0.0f)
        , dgrbsq1p(kHalfTileSamples, 0.0f)
        , dgrbsq1m(kHalfTileSamples, 0.0f)
        , vcd(kTileSamples, 0.0f)
        , hcd(kTileSamples, 0.0f)
        , vcdalt(kTileSamples, 0.0f)
        , hcdalt(kTileSamples, 0.0f)
        , dgintv(kTileSamples, 0.0f)
        , dginth(kTileSamples, 0.0f)
        , cddiffsq(kTileSamples, 0.0f)
        , hvwt(kHalfTileSamples, 0.0f)
        , nyquist(kHalfTileSamples, 0)
        , dgrb0(kHalfTileSamples, 0.0f)
        , dgrb1(kHalfTileSamples, 0.0f)
        , dgrb2h(kHalfTileSamples, 0.0f)
        , dgrb2v(kHalfTileSamples, 0.0f)
        , rbm(kHalfTileSamples, 0.0f)
        , rbp(kHalfTileSamples, 0.0f)
        , pmwt(kHalfTileSamples, 0.0f)
        , pmwtalt(kHalfTileSamples, 0.0f)
        , rbint(kHalfTileSamples, 0.0f)
    {
    }
};

struct DeviceBuffers
{
    uint16_t * raw = nullptr;
    float * cfa = nullptr;
    float * rgbgreen = nullptr;
    float * red = nullptr;
    float * green = nullptr;
    float * blue = nullptr;
    float * dirwts0 = nullptr;
    float * dirwts1 = nullptr;
    float * delhvsqsum = nullptr;
    float * delp = nullptr;
    float * delm = nullptr;
    float * dgrbsq1p = nullptr;
    float * dgrbsq1m = nullptr;
    float * vcd = nullptr;
    float * hcd = nullptr;
    float * vcdalt = nullptr;
    float * hcdalt = nullptr;
    float * variance_h0 = nullptr;
    float * variance_h1 = nullptr;
    float * variance_v0 = nullptr;
    float * variance_v1 = nullptr;
    unsigned char * variance_h_map = nullptr;
    unsigned char * variance_v_map = nullptr;
    unsigned char * variance_h_choice = nullptr;
    unsigned char * variance_v_choice = nullptr;
    float * dgintv = nullptr;
    float * dginth = nullptr;
    float * cddiffsq = nullptr;
    float * hvwt = nullptr;
    unsigned char * nyquist = nullptr;
    float * dgrb0 = nullptr;
    float * dgrb1 = nullptr;
    float * dgrb2h = nullptr;
    float * dgrb2v = nullptr;
    float * rbm = nullptr;
    float * rbp = nullptr;
    float * pmwt = nullptr;
    float * pmwtalt = nullptr;
    float * rbint = nullptr;
};

struct CompareStats
{
    std::size_t mismatches = 0;
    std::size_t bitMismatches = 0;
    float maxAbs = 0.0f;
    std::size_t maxIndex = 0;
};

struct TimedStageTotals
{
    int iterations = 0;
    int tiles = 0;
    double clearMs = 0.0;
    double loadMs = 0.0;
    double gradientsMs = 0.0;
    double diagonalPrecursorsMs = 0.0;
    double greenInterpolationMs = 0.0;
    double varianceSelectionMs = 0.0;
    double hvwtAdaptiveMs = 0.0;
    double nyquistTestMs = 0.0;
    double nyquistRefineMs = 0.0;
    double nyquistAreaMs = 0.0;
    double greenPlaneMs = 0.0;
    double nyquistGreenMs = 0.0;
    double diagonalRbMs = 0.0;
    double pmwtRbintMs = 0.0;
    double diagonalGreenMs = 0.0;
    double chromaCosetMs = 0.0;
    double fancyChromaMs = 0.0;
    double finalOutputMs = 0.0;
};

double timed_total_ms(const TimedStageTotals & totals)
{
    return totals.clearMs
        + totals.loadMs
        + totals.gradientsMs
        + totals.diagonalPrecursorsMs
        + totals.greenInterpolationMs
        + totals.varianceSelectionMs
        + totals.hvwtAdaptiveMs
        + totals.nyquistTestMs
        + totals.nyquistRefineMs
        + totals.nyquistAreaMs
        + totals.greenPlaneMs
        + totals.nyquistGreenMs
        + totals.diagonalRbMs
        + totals.pmwtRbintMs
        + totals.diagonalGreenMs
        + totals.chromaCosetMs
        + totals.fancyChromaMs
        + totals.finalOutputMs;
}

struct ByteCompareStats
{
    std::size_t mismatches = 0;
    std::size_t bitMismatches = 0;
    int maxAbs = 0;
    std::size_t maxIndex = 0;
};

__host__ __device__ int imin_i(int a, int b)
{
    return a < b ? a : b;
}

__host__ __device__ int reflect_index(int value, int limit)
{
    if (value < 0) return -value;
    if (value >= limit) return 2 * limit - value - 2;
    return value;
}

__host__ __device__ int fc_rggb(int row, int col)
{
    const int row2 = row & 1;
    const int col2 = col & 1;
    if (row2 == 0 && col2 == 0) return 0;
    if (row2 == 1 && col2 == 1) return 2;
    return 1;
}

__host__ __device__ float sqr_f(float value)
{
    return value * value;
}

__host__ __device__ float gauss_odd(int index)
{
    switch (index)
    {
        case 0: return 0.14659727707323927f;
        case 1: return 0.103592713382435f;
        case 2: return 0.0732036125103057f;
        default: return 0.0365543548389495f;
    }
}

__host__ __device__ float gauss_grad(int index)
{
    switch (index)
    {
        case 0: return 0.07384411893421103f;
        case 1: return 0.06207511968171489f;
        case 2: return 0.0521818194747806f;
        case 3: return 0.03687419286733595f;
        case 4: return 0.03099732204057846f;
        default: return 0.018413194161458882f;
    }
}

__host__ __device__ float gquinc(int index)
{
    switch (index)
    {
        case 0: return 0.169917f;
        case 1: return 0.108947f;
        case 2: return 0.069855f;
        default: return 0.0287182f;
    }
}

__host__ __device__ float gauss_seven(int index)
{
    return index == 0 ? 0.13719494435797422f : 0.05640252782101291f;
}

__host__ __device__ float min_f(float a, float b)
{
    return a < b ? a : b;
}

__host__ __device__ float max_f(float a, float b)
{
    return a > b ? a : b;
}

__host__ __device__ float coerce_f(float x, float lo, float hi)
{
    return max_f(min_f(x, hi), lo);
}

__host__ __device__ float ulim_f(float a, float b, float c)
{
    return (b < c) ? coerce_f(a, b, c) : coerce_f(a, c, b);
}

__host__ __device__ float xdiv2f_probe(float value)
{
    union Bits
    {
        float f;
        uint32_t u;
    };
    Bits bits = {value};
    if ((bits.u & 0x7fffffffu) != 0)
    {
        bits.u -= 1u << 23;
    }
    return bits.f;
}

__host__ __device__ float xmul2f_probe(float value)
{
    union Bits
    {
        float f;
        uint32_t u;
    };
    Bits bits = {value};
    if ((bits.u & 0x7fffffffu) != 0)
    {
        bits.u += 1u << 23;
    }
    return bits.f;
}

__host__ __device__ float xdivf_probe(float value, int exponentDelta)
{
    union Bits
    {
        float f;
        uint32_t u;
    };
    Bits bits = {value};
    if ((bits.u & 0x7fffffffu) != 0)
    {
        bits.u -= static_cast<uint32_t>(exponentDelta) << 23;
    }
    return bits.f;
}

__host__ __device__ float double_twice_div_probe(float value, float denominator)
{
    return static_cast<float>((static_cast<double>(value) * 2.0) /
                              static_cast<double>(denominator));
}

std::vector<uint16_t> make_raw_pattern(int width, int height, uint32_t seed)
{
    std::vector<uint16_t> raw(static_cast<std::size_t>(width) *
                              static_cast<std::size_t>(height));
    for (int y = 0; y < height; ++y)
    {
        for (int x = 0; x < width; ++x)
        {
            uint32_t v = seed;
            v ^= static_cast<uint32_t>(x + 1) * 0x45d9f3bu;
            v ^= static_cast<uint32_t>(y + 7) * 0x119de1f3u;
            v ^= (v >> 16);
            v *= 0x7feb352du;
            v ^= (v >> 15);
            raw[static_cast<std::size_t>(y) * width + x] =
                static_cast<uint16_t>(v & 0xffffu);
        }
    }
    return raw;
}

void cpu_stage_probe(const std::vector<uint16_t> & raw,
                     const CaseSpec & spec,
                     StageBuffers * out)
{
    const int bottom = imin_i(spec.top + kTileSize, spec.height + 16);
    const int right = imin_i(spec.left + kTileSize, spec.width + 16);
    const int rr1 = bottom - spec.top;
    const int cc1 = right - spec.left;
    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int p2 = -2 * kTileSize + 2;
    const int m2 = 2 * kTileSize + 2;
    const int p3 = -3 * kTileSize + 3;
    const int m3 = 3 * kTileSize + 3;
    int ex = 0;
    int ey = 0;
    if (fc_rggb(0, 0) == 1)
    {
        if (fc_rggb(0, 1) == 0)
        {
            ey = 0;
            ex = 1;
        }
        else
        {
            ey = 1;
            ex = 0;
        }
    }
    else
    {
        if (fc_rggb(0, 0) == 0)
        {
            ey = 0;
            ex = 0;
        }
        else
        {
            ey = 1;
            ex = 1;
        }
    }

    for (int rr = 0; rr < rr1; ++rr)
    {
        for (int cc = 0; cc < cc1; ++cc)
        {
            const int row = reflect_index(spec.top + rr, spec.height);
            const int col = reflect_index(spec.left + cc, spec.width);
            const int idx = rr * kTileSize + cc;
            const float normalized =
                static_cast<float>(raw[static_cast<std::size_t>(row) * spec.width + col]) /
                65535.0f;
            out->cfa[idx] = normalized;
            out->rgbgreen[idx] = (fc_rggb(rr, cc) == 1) ? normalized : 0.0f;
        }
    }

    for (int rr = 2; rr < rr1 - 2; ++rr)
    {
        for (int cc = 2; cc < cc1 - 2; ++cc)
        {
            const int idx = rr * kTileSize + cc;
            const float delh = std::fabs(out->cfa[idx + 1] - out->cfa[idx - 1]);
            const float delv = std::fabs(out->cfa[idx + v1] - out->cfa[idx - v1]);
            out->dirwts0[idx] =
                kEps + std::fabs(out->cfa[idx + v2] - out->cfa[idx]) +
                std::fabs(out->cfa[idx] - out->cfa[idx - v2]) + delv;
            out->dirwts1[idx] =
                kEps + std::fabs(out->cfa[idx + 2] - out->cfa[idx]) +
                std::fabs(out->cfa[idx] - out->cfa[idx - 2]) + delh;
            out->delhvsqsum[idx] = sqr_f(delh) + sqr_f(delv);
        }
    }

    for (int rr = 6; rr < rr1 - 6; ++rr)
    {
        for (int cc = 6, idx = rr * kTileSize + cc;
             cc < cc1 - 6;
             cc += 2, idx += 2)
        {
            const int halfIdx = idx >> 1;
            if ((fc_rggb(rr, 2) & 1) == 0)
            {
                out->delp[halfIdx] = std::fabs(out->cfa[idx + p1] - out->cfa[idx - p1]);
                out->delm[halfIdx] = std::fabs(out->cfa[idx + m1] - out->cfa[idx - m1]);
                out->dgrbsq1p[halfIdx] =
                    sqr_f(out->cfa[idx + 1] - out->cfa[idx + 1 - p1]) +
                    sqr_f(out->cfa[idx + 1] - out->cfa[idx + 1 + p1]);
                out->dgrbsq1m[halfIdx] =
                    sqr_f(out->cfa[idx + 1] - out->cfa[idx + 1 - m1]) +
                    sqr_f(out->cfa[idx + 1] - out->cfa[idx + 1 + m1]);
            }
            else
            {
                out->dgrbsq1p[halfIdx] =
                    sqr_f(out->cfa[idx] - out->cfa[idx - p1]) +
                    sqr_f(out->cfa[idx] - out->cfa[idx + p1]);
                out->dgrbsq1m[halfIdx] =
                    sqr_f(out->cfa[idx] - out->cfa[idx - m1]) +
                    sqr_f(out->cfa[idx] - out->cfa[idx + m1]);
                out->delp[halfIdx] =
                    std::fabs(out->cfa[idx + 1 + p1] - out->cfa[idx + 1 - p1]);
                out->delm[halfIdx] =
                    std::fabs(out->cfa[idx + 1 + m1] - out->cfa[idx + 1 - m1]);
            }
        }
    }

    for (int rr = 4; rr < rr1 - 4; ++rr)
    {
        for (int cc = 4; cc < cc1 - 4; ++cc)
        {
            const int idx = rr * kTileSize + cc;
            const float cru =
                out->cfa[idx - v1] * (out->dirwts0[idx - v2] + out->dirwts0[idx]) /
                (out->dirwts0[idx - v2] * (kEps + out->cfa[idx]) +
                 out->dirwts0[idx] * (kEps + out->cfa[idx - v2]));
            const float crd =
                out->cfa[idx + v1] * (out->dirwts0[idx + v2] + out->dirwts0[idx]) /
                (out->dirwts0[idx + v2] * (kEps + out->cfa[idx]) +
                 out->dirwts0[idx] * (kEps + out->cfa[idx + v2]));
            const float crl =
                out->cfa[idx - 1] * (out->dirwts1[idx - 2] + out->dirwts1[idx]) /
                (out->dirwts1[idx - 2] * (kEps + out->cfa[idx]) +
                 out->dirwts1[idx] * (kEps + out->cfa[idx - 2]));
            const float crr =
                out->cfa[idx + 1] * (out->dirwts1[idx + 2] + out->dirwts1[idx]) /
                (out->dirwts1[idx + 2] * (kEps + out->cfa[idx]) +
                 out->dirwts1[idx] * (kEps + out->cfa[idx + 2]));

            const float guha = out->cfa[idx - v1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx - v2]);
            const float gdha = out->cfa[idx + v1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx + v2]);
            const float glha = out->cfa[idx - 1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx - 2]);
            const float grha = out->cfa[idx + 1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx + 2]);

            float guar = (std::fabs(1.0f - cru) < kAdaptiveRatioThreshold) ? out->cfa[idx] * cru : guha;
            float gdar = (std::fabs(1.0f - crd) < kAdaptiveRatioThreshold) ? out->cfa[idx] * crd : gdha;
            float glar = (std::fabs(1.0f - crl) < kAdaptiveRatioThreshold) ? out->cfa[idx] * crl : glha;
            float grar = (std::fabs(1.0f - crr) < kAdaptiveRatioThreshold) ? out->cfa[idx] * crr : grha;

            const float hwt = out->dirwts1[idx - 1] / (out->dirwts1[idx - 1] + out->dirwts1[idx + 1]);
            const float vwt = out->dirwts0[idx - v1] / (out->dirwts0[idx + v1] + out->dirwts0[idx - v1]);
            const float gintvha = vwt * gdha + (1.0f - vwt) * guha;
            const float ginthha = hwt * grha + (1.0f - hwt) * glha;
            const float vinterp = vwt * gdar + (1.0f - vwt) * guar;
            const float hinterp = hwt * grar + (1.0f - hwt) * glar;

            if ((fc_rggb(rr, cc) & 1) != 0)
            {
                out->vcd[idx] = out->cfa[idx] - vinterp;
                out->hcd[idx] = out->cfa[idx] - hinterp;
                out->vcdalt[idx] = out->cfa[idx] - gintvha;
                out->hcdalt[idx] = out->cfa[idx] - ginthha;
            }
            else
            {
                out->vcd[idx] = vinterp - out->cfa[idx];
                out->hcd[idx] = hinterp - out->cfa[idx];
                out->vcdalt[idx] = gintvha - out->cfa[idx];
                out->hcdalt[idx] = ginthha - out->cfa[idx];
            }

            if (out->cfa[idx] > kClipPoint8 || gintvha > kClipPoint8 || ginthha > kClipPoint8)
            {
                guar = guha;
                gdar = gdha;
                glar = glha;
                grar = grha;
                out->vcd[idx] = out->vcdalt[idx];
                out->hcd[idx] = out->hcdalt[idx];
            }

            out->dgintv[idx] = std::min(sqr_f(guha - gdha), sqr_f(guar - gdar));
            out->dginth[idx] = std::min(sqr_f(glha - grha), sqr_f(glar - grar));
        }
    }

    for (int rr = 4; rr < rr1 - 4; ++rr)
    {
        for (int cc = 4; cc < cc1 - 4; ++cc)
        {
            const int idx = rr * kTileSize + cc;
            const float hcdvar =
                3.0f * (sqr_f(out->hcd[idx - 2]) +
                        sqr_f(out->hcd[idx]) +
                        sqr_f(out->hcd[idx + 2])) -
                sqr_f(out->hcd[idx - 2] + out->hcd[idx] + out->hcd[idx + 2]);
            const float hcdaltvar =
                3.0f * (sqr_f(out->hcdalt[idx - 2]) +
                        sqr_f(out->hcdalt[idx]) +
                        sqr_f(out->hcdalt[idx + 2])) -
                sqr_f(out->hcdalt[idx - 2] + out->hcdalt[idx] + out->hcdalt[idx + 2]);
            const float vcdvar =
                3.0f * (sqr_f(out->vcd[idx - v2]) +
                        sqr_f(out->vcd[idx]) +
                        sqr_f(out->vcd[idx + v2])) -
                sqr_f(out->vcd[idx - v2] + out->vcd[idx] + out->vcd[idx + v2]);
            const float vcdaltvar =
                3.0f * (sqr_f(out->vcdalt[idx - v2]) +
                        sqr_f(out->vcdalt[idx]) +
                        sqr_f(out->vcdalt[idx + v2])) -
                sqr_f(out->vcdalt[idx - v2] + out->vcdalt[idx] + out->vcdalt[idx + v2]);

            if (hcdaltvar < hcdvar) out->hcd[idx] = out->hcdalt[idx];
            if (vcdaltvar < vcdvar) out->vcd[idx] = out->vcdalt[idx];

            if ((fc_rggb(rr, cc) & 1) != 0)
            {
                const float ginth = -out->hcd[idx] + out->cfa[idx];
                const float gintv = -out->vcd[idx] + out->cfa[idx];
                if (out->hcd[idx] > 0.0f)
                {
                    const float bounded = -ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]) + out->cfa[idx];
                    if (3.0f * out->hcd[idx] > (ginth + out->cfa[idx]))
                    {
                        out->hcd[idx] = bounded;
                    }
                    else
                    {
                        const float hwt = 1.0f - 3.0f * out->hcd[idx] / (kEps + ginth + out->cfa[idx]);
                        out->hcd[idx] = hwt * out->hcd[idx] + (1.0f - hwt) * bounded;
                    }
                }
                if (out->vcd[idx] > 0.0f)
                {
                    const float bounded = -ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]) + out->cfa[idx];
                    if (3.0f * out->vcd[idx] > (gintv + out->cfa[idx]))
                    {
                        out->vcd[idx] = bounded;
                    }
                    else
                    {
                        const float vwt = 1.0f - 3.0f * out->vcd[idx] / (kEps + gintv + out->cfa[idx]);
                        out->vcd[idx] = vwt * out->vcd[idx] + (1.0f - vwt) * bounded;
                    }
                }
                if (ginth > kClipPoint)
                {
                    out->hcd[idx] = -ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]) + out->cfa[idx];
                }
                if (gintv > kClipPoint)
                {
                    out->vcd[idx] = -ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]) + out->cfa[idx];
                }
            }
            else
            {
                const float ginth = out->hcd[idx] + out->cfa[idx];
                const float gintv = out->vcd[idx] + out->cfa[idx];
                if (out->hcd[idx] < 0.0f)
                {
                    const float bounded = ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]) - out->cfa[idx];
                    if (3.0f * out->hcd[idx] < -(ginth + out->cfa[idx]))
                    {
                        out->hcd[idx] = bounded;
                    }
                    else
                    {
                        const float hwt = 1.0f + 3.0f * out->hcd[idx] / (kEps + ginth + out->cfa[idx]);
                        out->hcd[idx] = hwt * out->hcd[idx] + (1.0f - hwt) * bounded;
                    }
                }
                if (out->vcd[idx] < 0.0f)
                {
                    const float bounded = ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]) - out->cfa[idx];
                    if (3.0f * out->vcd[idx] < -(gintv + out->cfa[idx]))
                    {
                        out->vcd[idx] = bounded;
                    }
                    else
                    {
                        const float vwt = 1.0f + 3.0f * out->vcd[idx] / (kEps + gintv + out->cfa[idx]);
                        out->vcd[idx] = vwt * out->vcd[idx] + (1.0f - vwt) * bounded;
                    }
                }
                if (ginth > kClipPoint)
                {
                    out->hcd[idx] = ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]) - out->cfa[idx];
                }
                if (gintv > kClipPoint)
                {
                    out->vcd[idx] = ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]) - out->cfa[idx];
                }
                out->cddiffsq[idx] = sqr_f(out->vcd[idx] - out->hcd[idx]);
            }
        }
    }

    const int v3 = 3 * kTileSize;
    const float epssq = kEps * kEps;
    for (int rr = 6; rr < rr1 - 6; ++rr)
    {
        for (int cc = 6 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 6;
             cc += 2, idx += 2)
        {
            const float uave =
                out->vcd[idx] + out->vcd[idx - v1] + out->vcd[idx - v2] + out->vcd[idx - v3];
            const float dave =
                out->vcd[idx] + out->vcd[idx + v1] + out->vcd[idx + v2] + out->vcd[idx + v3];
            const float lave =
                out->hcd[idx] + out->hcd[idx - 1] + out->hcd[idx - 2] + out->hcd[idx - 3];
            const float rave =
                out->hcd[idx] + out->hcd[idx + 1] + out->hcd[idx + 2] + out->hcd[idx + 3];

            const float dgrbvvaru =
                sqr_f(out->vcd[idx] - uave) +
                sqr_f(out->vcd[idx - v1] - uave) +
                sqr_f(out->vcd[idx - v2] - uave) +
                sqr_f(out->vcd[idx - v3] - uave);
            const float dgrbvvard =
                sqr_f(out->vcd[idx] - dave) +
                sqr_f(out->vcd[idx + v1] - dave) +
                sqr_f(out->vcd[idx + v2] - dave) +
                sqr_f(out->vcd[idx + v3] - dave);
            const float dgrbhvarl =
                sqr_f(out->hcd[idx] - lave) +
                sqr_f(out->hcd[idx - 1] - lave) +
                sqr_f(out->hcd[idx - 2] - lave) +
                sqr_f(out->hcd[idx - 3] - lave);
            const float dgrbhvarr =
                sqr_f(out->hcd[idx] - rave) +
                sqr_f(out->hcd[idx + 1] - rave) +
                sqr_f(out->hcd[idx + 2] - rave) +
                sqr_f(out->hcd[idx + 3] - rave);

            const float hwt = out->dirwts1[idx - 1] /
                              (out->dirwts1[idx - 1] + out->dirwts1[idx + 1]);
            const float vwt = out->dirwts0[idx - v1] /
                              (out->dirwts0[idx + v1] + out->dirwts0[idx - v1]);

            const float vcdvar = epssq + vwt * dgrbvvard + (1.0f - vwt) * dgrbvvaru;
            const float hcdvar = epssq + hwt * dgrbhvarr + (1.0f - hwt) * dgrbhvarl;

            const float dgrbvvaru1 =
                out->dgintv[idx] + out->dgintv[idx - v1] + out->dgintv[idx - v2];
            const float dgrbvvard1 =
                out->dgintv[idx] + out->dgintv[idx + v1] + out->dgintv[idx + v2];
            const float dgrbhvarl1 =
                out->dginth[idx] + out->dginth[idx - 1] + out->dginth[idx - 2];
            const float dgrbhvarr1 =
                out->dginth[idx] + out->dginth[idx + 1] + out->dginth[idx + 2];

            const float vcdvar1 = epssq + vwt * dgrbvvard1 + (1.0f - vwt) * dgrbvvaru1;
            const float hcdvar1 = epssq + hwt * dgrbhvarr1 + (1.0f - hwt) * dgrbhvarl1;

            const float varwt = hcdvar / (vcdvar + hcdvar);
            const float diffwt = hcdvar1 / (vcdvar1 + hcdvar1);
            if ((0.5f - varwt) * (0.5f - diffwt) > 0.0f &&
                std::fabs(0.5f - diffwt) < std::fabs(0.5f - varwt))
            {
                out->hvwt[idx >> 1] = varwt;
            }
            else
            {
                out->hvwt[idx >> 1] = diffwt;
            }
        }
    }

    for (int rr = 6; rr < rr1 - 6; ++rr)
    {
        for (int cc = 6 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 6;
             cc += 2, idx += 2)
        {
            float nyqtest =
                gauss_odd(0) * out->cddiffsq[idx] +
                gauss_odd(1) * (out->cddiffsq[idx - m1] + out->cddiffsq[idx + p1] +
                                out->cddiffsq[idx - p1] + out->cddiffsq[idx + m1]) +
                gauss_odd(2) * (out->cddiffsq[idx - v2] + out->cddiffsq[idx - 2] +
                                out->cddiffsq[idx + 2] + out->cddiffsq[idx + v2]) +
                gauss_odd(3) * (out->cddiffsq[idx - m2] + out->cddiffsq[idx + p2] +
                                out->cddiffsq[idx - p2] + out->cddiffsq[idx + m2]);

            nyqtest -=
                kNyquistThreshold *
                (gauss_grad(0) * out->delhvsqsum[idx] +
                 gauss_grad(1) * (out->delhvsqsum[idx - v1] + out->delhvsqsum[idx + 1] +
                                  out->delhvsqsum[idx - 1] + out->delhvsqsum[idx + v1]) +
                 gauss_grad(2) * (out->delhvsqsum[idx - m1] + out->delhvsqsum[idx + p1] +
                                  out->delhvsqsum[idx - p1] + out->delhvsqsum[idx + m1]) +
                 gauss_grad(3) * (out->delhvsqsum[idx - v2] + out->delhvsqsum[idx - 2] +
                                  out->delhvsqsum[idx + 2] + out->delhvsqsum[idx + v2]) +
                 gauss_grad(4) * (out->delhvsqsum[idx - 2 * kTileSize - 1] +
                                  out->delhvsqsum[idx - 2 * kTileSize + 1] +
                                  out->delhvsqsum[idx - kTileSize - 2] +
                                  out->delhvsqsum[idx - kTileSize + 2] +
                                  out->delhvsqsum[idx + kTileSize - 2] +
                                  out->delhvsqsum[idx + kTileSize + 2] +
                                  out->delhvsqsum[idx + 2 * kTileSize - 1] +
                                  out->delhvsqsum[idx + 2 * kTileSize + 1]) +
                 gauss_grad(5) * (out->delhvsqsum[idx - m2] + out->delhvsqsum[idx + p2] +
                                  out->delhvsqsum[idx - p2] + out->delhvsqsum[idx + m2]));

            if (nyqtest > 0.0f)
            {
                out->nyquist[idx >> 1] = 1;
            }
        }
    }

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        for (int cc = 8 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2, idx += 2)
        {
            const unsigned int nyquisttemp =
                out->nyquist[(idx - v2) >> 1] +
                out->nyquist[(idx - m1) >> 1] +
                out->nyquist[(idx + p1) >> 1] +
                out->nyquist[(idx - 2) >> 1] +
                out->nyquist[idx >> 1] +
                out->nyquist[(idx + 2) >> 1] +
                out->nyquist[(idx - p1) >> 1] +
                out->nyquist[(idx + m1) >> 1] +
                out->nyquist[(idx + v2) >> 1];

            if (nyquisttemp > 4) out->nyquist[idx >> 1] = 1;
            if (nyquisttemp < 4) out->nyquist[idx >> 1] = 0;
        }
    }

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        for (int cc = 8 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2, idx += 2)
        {
            if (out->nyquist[idx >> 1])
            {
                float sumh = 0.0f;
                float sumv = 0.0f;
                float sumsqh = 0.0f;
                float sumsqv = 0.0f;
                float areawt = 0.0f;
                for (int i = -6; i < 7; i += 2)
                {
                    for (int j = -6; j < 7; j += 2)
                    {
                        const int idx1 = (rr + i) * kTileSize + cc + j;
                        if (out->nyquist[idx1 >> 1])
                        {
                            sumh +=
                                out->cfa[idx1] -
                                xdiv2f_probe(out->cfa[idx1 - 1] + out->cfa[idx1 + 1]);
                            sumv +=
                                out->cfa[idx1] -
                                xdiv2f_probe(out->cfa[idx1 - v1] + out->cfa[idx1 + v1]);
                            sumsqh +=
                                xdiv2f_probe(sqr_f(out->cfa[idx1] - out->cfa[idx1 - 1]) +
                                             sqr_f(out->cfa[idx1] - out->cfa[idx1 + 1]));
                            sumsqv +=
                                xdiv2f_probe(sqr_f(out->cfa[idx1] - out->cfa[idx1 - v1]) +
                                             sqr_f(out->cfa[idx1] - out->cfa[idx1 + v1]));
                            areawt += 1.0f;
                        }
                    }
                }

                const float hcdvar = epssq + fabsf(areawt * sumsqh - sumh * sumh);
                const float vcdvar = epssq + fabsf(areawt * sumsqv - sumv * sumv);
                out->hvwt[idx >> 1] = hcdvar / (vcdvar + hcdvar);
            }
        }
    }

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        for (int cc = 8 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2, idx += 2)
        {
            const float hvwtalt =
                xdivf_probe(out->hvwt[(idx - m1) >> 1] +
                            out->hvwt[(idx + p1) >> 1] +
                            out->hvwt[(idx - p1) >> 1] +
                            out->hvwt[(idx + m1) >> 1],
                            2);
            if (fabsf(0.5f - out->hvwt[idx >> 1]) < fabsf(0.5f - hvwtalt))
            {
                out->hvwt[idx >> 1] = hvwtalt;
            }

            out->dgrb0[idx >> 1] =
                out->hcd[idx] * (1.0f - out->hvwt[idx >> 1]) +
                out->vcd[idx] * out->hvwt[idx >> 1];
            out->rgbgreen[idx] = out->cfa[idx] + out->dgrb0[idx >> 1];

            if (out->nyquist[idx >> 1])
            {
                out->dgrb2h[idx >> 1] =
                    sqr_f(out->rgbgreen[idx] -
                          xdiv2f_probe(out->rgbgreen[idx - 1] + out->rgbgreen[idx + 1]));
                out->dgrb2v[idx >> 1] =
                    sqr_f(out->rgbgreen[idx] -
                          xdiv2f_probe(out->rgbgreen[idx - v1] + out->rgbgreen[idx + v1]));
            }
            else
            {
                out->dgrb2h[idx >> 1] = 0.0f;
                out->dgrb2v[idx >> 1] = 0.0f;
            }
        }
    }

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        for (int cc = 8 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2, idx += 2)
        {
            if (out->nyquist[idx >> 1])
            {
                const float gvarh =
                    epssq +
                    (gquinc(0) * out->dgrb2h[idx >> 1] +
                     gquinc(1) * (out->dgrb2h[(idx - m1) >> 1] +
                                  out->dgrb2h[(idx + p1) >> 1] +
                                  out->dgrb2h[(idx - p1) >> 1] +
                                  out->dgrb2h[(idx + m1) >> 1]) +
                     gquinc(2) * (out->dgrb2h[(idx - v2) >> 1] +
                                  out->dgrb2h[(idx - 2) >> 1] +
                                  out->dgrb2h[(idx + 2) >> 1] +
                                  out->dgrb2h[(idx + v2) >> 1]) +
                     gquinc(3) * (out->dgrb2h[(idx - m2) >> 1] +
                                  out->dgrb2h[(idx + p2) >> 1] +
                                  out->dgrb2h[(idx - p2) >> 1] +
                                  out->dgrb2h[(idx + m2) >> 1]));
                const float gvarv =
                    epssq +
                    (gquinc(0) * out->dgrb2v[idx >> 1] +
                     gquinc(1) * (out->dgrb2v[(idx - m1) >> 1] +
                                  out->dgrb2v[(idx + p1) >> 1] +
                                  out->dgrb2v[(idx - p1) >> 1] +
                                  out->dgrb2v[(idx + m1) >> 1]) +
                     gquinc(2) * (out->dgrb2v[(idx - v2) >> 1] +
                                  out->dgrb2v[(idx - 2) >> 1] +
                                  out->dgrb2v[(idx + 2) >> 1] +
                                  out->dgrb2v[(idx + v2) >> 1]) +
                     gquinc(3) * (out->dgrb2v[(idx - m2) >> 1] +
                                  out->dgrb2v[(idx + p2) >> 1] +
                                  out->dgrb2v[(idx - p2) >> 1] +
                                  out->dgrb2v[(idx + m2) >> 1]));

                out->dgrb0[idx >> 1] =
                    (out->hcd[idx] * gvarv + out->vcd[idx] * gvarh) / (gvarv + gvarh);
                out->rgbgreen[idx] = out->cfa[idx] + out->dgrb0[idx >> 1];
            }
        }
    }

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        for (int cc = 8 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2, idx += 2)
        {
            const int halfIdx = idx >> 1;
            const float crse = xmul2f_probe(out->cfa[idx + m1]) /
                               (kEps + out->cfa[idx] + out->cfa[idx + m2]);
            const float crnw = xmul2f_probe(out->cfa[idx - m1]) /
                               (kEps + out->cfa[idx] + out->cfa[idx - m2]);
            const float crne = xmul2f_probe(out->cfa[idx + p1]) /
                               (kEps + out->cfa[idx] + out->cfa[idx + p2]);
            const float crsw = xmul2f_probe(out->cfa[idx - p1]) /
                               (kEps + out->cfa[idx] + out->cfa[idx - p2]);

            const float rbse =
                (std::fabs(1.0f - crse) < kAdaptiveRatioThreshold)
                    ? out->cfa[idx] * crse
                    : out->cfa[idx + m1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx + m2]);
            const float rbnw =
                (std::fabs(1.0f - crnw) < kAdaptiveRatioThreshold)
                    ? out->cfa[idx] * crnw
                    : out->cfa[idx - m1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx - m2]);
            const float rbne =
                (std::fabs(1.0f - crne) < kAdaptiveRatioThreshold)
                    ? out->cfa[idx] * crne
                    : out->cfa[idx + p1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx + p2]);
            const float rbsw =
                (std::fabs(1.0f - crsw) < kAdaptiveRatioThreshold)
                    ? out->cfa[idx] * crsw
                    : out->cfa[idx - p1] + xdiv2f_probe(out->cfa[idx] - out->cfa[idx - p2]);

            const float wtse = kEps + out->delm[halfIdx] + out->delm[(idx + m1) >> 1] +
                               out->delm[(idx + m2) >> 1];
            const float wtnw = kEps + out->delm[halfIdx] + out->delm[(idx - m1) >> 1] +
                               out->delm[(idx - m2) >> 1];
            const float wtne = kEps + out->delp[halfIdx] + out->delp[(idx + p1) >> 1] +
                               out->delp[(idx + p2) >> 1];
            const float wtsw = kEps + out->delp[halfIdx] + out->delp[(idx - p1) >> 1] +
                               out->delp[(idx - p2) >> 1];

            out->rbm[halfIdx] = (wtse * rbnw + wtnw * rbse) / (wtse + wtnw);
            out->rbp[halfIdx] = (wtne * rbsw + wtsw * rbne) / (wtne + wtsw);

            const float rbvarm =
                epssq +
                (gauss_seven(0) * (out->dgrbsq1m[(idx - v1) >> 1] +
                                   out->dgrbsq1m[(idx - 1) >> 1] +
                                   out->dgrbsq1m[(idx + 1) >> 1] +
                                   out->dgrbsq1m[(idx + v1) >> 1]) +
                 gauss_seven(1) * (out->dgrbsq1m[(idx - v2 - 1) >> 1] +
                                   out->dgrbsq1m[(idx - v2 + 1) >> 1] +
                                   out->dgrbsq1m[(idx - 2 - v1) >> 1] +
                                   out->dgrbsq1m[(idx + 2 - v1) >> 1] +
                                   out->dgrbsq1m[(idx - 2 + v1) >> 1] +
                                   out->dgrbsq1m[(idx + 2 + v1) >> 1] +
                                   out->dgrbsq1m[(idx + v2 - 1) >> 1] +
                                   out->dgrbsq1m[(idx + v2 + 1) >> 1]));
            const float rbvarp =
                epssq +
                (gauss_seven(0) * (out->dgrbsq1p[(idx - v1) >> 1] +
                                   out->dgrbsq1p[(idx - 1) >> 1] +
                                   out->dgrbsq1p[(idx + 1) >> 1] +
                                   out->dgrbsq1p[(idx + v1) >> 1]) +
                 gauss_seven(1) * (out->dgrbsq1p[(idx - v2 - 1) >> 1] +
                                   out->dgrbsq1p[(idx - v2 + 1) >> 1] +
                                   out->dgrbsq1p[(idx - 2 - v1) >> 1] +
                                   out->dgrbsq1p[(idx + 2 - v1) >> 1] +
                                   out->dgrbsq1p[(idx - 2 + v1) >> 1] +
                                   out->dgrbsq1p[(idx + 2 + v1) >> 1] +
                                   out->dgrbsq1p[(idx + v2 - 1) >> 1] +
                                   out->dgrbsq1p[(idx + v2 + 1) >> 1]));
            out->pmwt[halfIdx] = rbvarm / (rbvarp + rbvarm);

            if (out->rbp[halfIdx] < out->cfa[idx])
            {
                const float bounded = ulim_f(out->rbp[halfIdx], out->cfa[idx - p1], out->cfa[idx + p1]);
                if (xmul2f_probe(out->rbp[halfIdx]) < out->cfa[idx])
                {
                    out->rbp[halfIdx] = bounded;
                }
                else
                {
                    const float pwt =
                        xmul2f_probe(out->cfa[idx] - out->rbp[halfIdx]) /
                        (kEps + out->rbp[halfIdx] + out->cfa[idx]);
                    out->rbp[halfIdx] = pwt * out->rbp[halfIdx] + (1.0f - pwt) * bounded;
                }
            }
            if (out->rbm[halfIdx] < out->cfa[idx])
            {
                const float bounded = ulim_f(out->rbm[halfIdx], out->cfa[idx - m1], out->cfa[idx + m1]);
                if (xmul2f_probe(out->rbm[halfIdx]) < out->cfa[idx])
                {
                    out->rbm[halfIdx] = bounded;
                }
                else
                {
                    const float mwt =
                        xmul2f_probe(out->cfa[idx] - out->rbm[halfIdx]) /
                        (kEps + out->rbm[halfIdx] + out->cfa[idx]);
                    out->rbm[halfIdx] = mwt * out->rbm[halfIdx] + (1.0f - mwt) * bounded;
                }
            }

            if (out->rbp[halfIdx] > kClipPoint)
            {
                out->rbp[halfIdx] = ulim_f(out->rbp[halfIdx], out->cfa[idx - p1], out->cfa[idx + p1]);
            }
            if (out->rbm[halfIdx] > kClipPoint)
            {
                out->rbm[halfIdx] = ulim_f(out->rbm[halfIdx], out->cfa[idx - m1], out->cfa[idx + m1]);
            }
        }
    }

    for (int rr = 10; rr < rr1 - 10; ++rr)
    {
        for (int cc = 10 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 10;
             cc += 2, idx += 2)
        {
            const int halfIdx = idx >> 1;
            out->pmwtalt[halfIdx] =
                xdivf_probe(out->pmwt[(idx - m1) >> 1] +
                                out->pmwt[(idx + p1) >> 1] +
                                out->pmwt[(idx - p1) >> 1] +
                                out->pmwt[(idx + m1) >> 1],
                            2);
            if (std::fabs(0.5f - out->pmwt[halfIdx]) < std::fabs(0.5f - out->pmwtalt[halfIdx]))
            {
                out->pmwt[halfIdx] = out->pmwtalt[halfIdx];
            }

            out->rbint[halfIdx] =
                xdiv2f_probe(out->cfa[idx] +
                             out->rbm[halfIdx] * (1.0f - out->pmwt[halfIdx]) +
                             out->rbp[halfIdx] * out->pmwt[halfIdx]);
        }
    }

    for (int rr = 12; rr < rr1 - 12; ++rr)
    {
        for (int cc = 12 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 12;
             cc += 2, idx += 2)
        {
            const int halfIdx = idx >> 1;
            if (std::fabs(0.5f - out->pmwt[halfIdx]) < std::fabs(0.5f - out->hvwt[halfIdx]))
            {
                continue;
            }

            const float cruDenom = kEps + out->rbint[halfIdx] + out->rbint[halfIdx - v1];
            const float crdDenom = kEps + out->rbint[halfIdx] + out->rbint[halfIdx + v1];
            const float crlDenom = kEps + out->rbint[halfIdx] + out->rbint[halfIdx - 1];
            const float crrDenom = kEps + out->rbint[halfIdx] + out->rbint[halfIdx + 1];
            const float cru = double_twice_div_probe(out->cfa[idx - v1], cruDenom);
            const float crd = double_twice_div_probe(out->cfa[idx + v1], crdDenom);
            const float crl = double_twice_div_probe(out->cfa[idx - 1], crlDenom);
            const float crr = double_twice_div_probe(out->cfa[idx + 1], crrDenom);

            const float gu =
                (std::fabs(1.0f - cru) < kAdaptiveRatioThreshold)
                    ? out->rbint[halfIdx] * cru
                    : out->cfa[idx - v1] + xdiv2f_probe(out->rbint[halfIdx] - out->rbint[halfIdx - v1]);
            const float gd =
                (std::fabs(1.0f - crd) < kAdaptiveRatioThreshold)
                    ? out->rbint[halfIdx] * crd
                    : out->cfa[idx + v1] + xdiv2f_probe(out->rbint[halfIdx] - out->rbint[halfIdx + v1]);
            const float gl =
                (std::fabs(1.0f - crl) < kAdaptiveRatioThreshold)
                    ? out->rbint[halfIdx] * crl
                    : out->cfa[idx - 1] + xdiv2f_probe(out->rbint[halfIdx] - out->rbint[halfIdx - 1]);
            const float gr =
                (std::fabs(1.0f - crr) < kAdaptiveRatioThreshold)
                    ? out->rbint[halfIdx] * crr
                    : out->cfa[idx + 1] + xdiv2f_probe(out->rbint[halfIdx] - out->rbint[halfIdx + 1]);

            float gintv =
                (out->dirwts0[idx - v1] * gd + out->dirwts0[idx + v1] * gu) /
                (out->dirwts0[idx + v1] + out->dirwts0[idx - v1]);
            float ginth =
                (out->dirwts1[idx - 1] * gr + out->dirwts1[idx + 1] * gl) /
                (out->dirwts1[idx - 1] + out->dirwts1[idx + 1]);

            if (gintv < out->rbint[halfIdx])
            {
                if (2.0f * gintv < out->rbint[halfIdx])
                {
                    gintv = ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]);
                }
                else
                {
                    const float diff = out->rbint[halfIdx] - gintv;
                    const float denom = kEps + gintv + out->rbint[halfIdx];
                    const float vwt = double_twice_div_probe(diff, denom);
                    gintv = vwt * gintv +
                            (1.0f - vwt) * ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]);
                }
            }
            if (ginth < out->rbint[halfIdx])
            {
                if (2.0f * ginth < out->rbint[halfIdx])
                {
                    ginth = ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]);
                }
                else
                {
                    const float diff = out->rbint[halfIdx] - ginth;
                    const float denom = kEps + ginth + out->rbint[halfIdx];
                    const float hwt = double_twice_div_probe(diff, denom);
                    ginth = hwt * ginth +
                            (1.0f - hwt) * ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]);
                }
            }

            if (ginth > kClipPoint)
            {
                ginth = ulim_f(ginth, out->cfa[idx - 1], out->cfa[idx + 1]);
            }
            if (gintv > kClipPoint)
            {
                gintv = ulim_f(gintv, out->cfa[idx - v1], out->cfa[idx + v1]);
            }

            out->rgbgreen[idx] = ginth * (1.0f - out->hvwt[halfIdx]) + gintv * out->hvwt[halfIdx];
            out->dgrb0[halfIdx] = out->rgbgreen[idx] - out->cfa[idx];
        }
    }

    for (int rr = 13 - ey; rr < rr1 - 12; rr += 2)
    {
        for (int cc = 13 - ex, halfIdx = (rr * kTileSize + cc) >> 1;
             cc < cc1 - 12;
             cc += 2, ++halfIdx)
        {
            out->dgrb1[halfIdx] = out->dgrb0[halfIdx];
            out->dgrb0[halfIdx] = 0.0f;
        }
    }

    for (int rr = 14; rr < rr1 - 14; ++rr)
    {
        for (int cc = 14 + (fc_rggb(rr, 2) & 1), idx = rr * kTileSize + cc;
             cc < cc1 - 14;
             cc += 2, idx += 2)
        {
            const int channel = 1 - fc_rggb(rr, cc) / 2;
            std::vector<float> & dgrb = (channel == 0) ? out->dgrb0 : out->dgrb1;

            const float wtnw =
                1.0f /
                (kEps +
                 std::fabs(dgrb[(idx - m1) >> 1] - dgrb[(idx + m1) >> 1]) +
                 std::fabs(dgrb[(idx - m1) >> 1] - dgrb[(idx - m3) >> 1]) +
                 std::fabs(dgrb[(idx + m1) >> 1] - dgrb[(idx - m3) >> 1]));
            const float wtne =
                1.0f /
                (kEps +
                 std::fabs(dgrb[(idx + p1) >> 1] - dgrb[(idx - p1) >> 1]) +
                 std::fabs(dgrb[(idx + p1) >> 1] - dgrb[(idx + p3) >> 1]) +
                 std::fabs(dgrb[(idx - p1) >> 1] - dgrb[(idx + p3) >> 1]));
            const float wtsw =
                1.0f /
                (kEps +
                 std::fabs(dgrb[(idx - p1) >> 1] - dgrb[(idx + p1) >> 1]) +
                 std::fabs(dgrb[(idx - p1) >> 1] - dgrb[(idx + m3) >> 1]) +
                 std::fabs(dgrb[(idx + p1) >> 1] - dgrb[(idx - p3) >> 1]));
            const float wtse =
                1.0f /
                (kEps +
                 std::fabs(dgrb[(idx + m1) >> 1] - dgrb[(idx - m1) >> 1]) +
                 std::fabs(dgrb[(idx + m1) >> 1] - dgrb[(idx - p3) >> 1]) +
                 std::fabs(dgrb[(idx - m1) >> 1] - dgrb[(idx + m3) >> 1]));

            dgrb[idx >> 1] =
                (wtnw * (1.325f * dgrb[(idx - m1) >> 1] -
                         0.175f * dgrb[(idx - m3) >> 1] -
                         0.075f * dgrb[(idx - m1 - 2) >> 1] -
                         0.075f * dgrb[(idx - m1 - v2) >> 1]) +
                 wtne * (1.325f * dgrb[(idx + p1) >> 1] -
                         0.175f * dgrb[(idx + p3) >> 1] -
                         0.075f * dgrb[(idx + p1 + 2) >> 1] -
                         0.075f * dgrb[(idx + p1 + v2) >> 1]) +
                 wtsw * (1.325f * dgrb[(idx - p1) >> 1] -
                         0.175f * dgrb[(idx - p3) >> 1] -
                         0.075f * dgrb[(idx - p1 - 2) >> 1] -
                         0.075f * dgrb[(idx - p1 - v2) >> 1]) +
                 wtse * (1.325f * dgrb[(idx + m1) >> 1] -
                         0.175f * dgrb[(idx + m3) >> 1] -
                         0.075f * dgrb[(idx + m1 + 2) >> 1] -
                         0.075f * dgrb[(idx + m1 + v2) >> 1])) /
                (wtnw + wtne + wtsw + wtse);
        }
    }

    for (int rr = 16; rr < rr1 - 16; ++rr)
    {
        if ((fc_rggb(rr, 2) & 1) == 1)
        {
            int cc = 16;
            int idx = rr * kTileSize + cc;
            for (; cc < cc1 - 16 - (cc1 & 1); cc += 2, ++idx)
            {
                float temp =
                    1.0f /
                    (out->hvwt[(idx - v1) >> 1] +
                     (1.0f - out->hvwt[(idx + 1) >> 1]) +
                     (1.0f - out->hvwt[(idx - 1) >> 1]) +
                     out->hvwt[(idx + v1) >> 1]);
                out->red[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb0[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb0[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb0[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb0[(idx + v1) >> 1]) *
                         temp);
                out->blue[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb1[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb1[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb1[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb1[(idx + v1) >> 1]) *
                         temp);

                ++idx;
                out->red[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb0[idx >> 1]);
                out->blue[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb1[idx >> 1]);
            }
            if (cc1 & 1)
            {
                float temp =
                    1.0f /
                    (out->hvwt[(idx - v1) >> 1] +
                     (1.0f - out->hvwt[(idx + 1) >> 1]) +
                     (1.0f - out->hvwt[(idx - 1) >> 1]) +
                     out->hvwt[(idx + v1) >> 1]);
                out->red[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb0[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb0[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb0[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb0[(idx + v1) >> 1]) *
                         temp);
                out->blue[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb1[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb1[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb1[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb1[(idx + v1) >> 1]) *
                         temp);
            }
        }
        else
        {
            int cc = 16;
            int idx = rr * kTileSize + cc;
            for (; cc < cc1 - 16 - (cc1 & 1); cc += 2, ++idx)
            {
                out->red[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb0[idx >> 1]);
                out->blue[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb1[idx >> 1]);

                ++idx;
                float temp =
                    1.0f /
                    (out->hvwt[(idx - v1) >> 1] +
                     (1.0f - out->hvwt[(idx + 1) >> 1]) +
                     (1.0f - out->hvwt[(idx - 1) >> 1]) +
                     out->hvwt[(idx + v1) >> 1]);
                out->red[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb0[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb0[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb0[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb0[(idx + v1) >> 1]) *
                         temp);
                out->blue[idx] =
                    65535.0f *
                    (out->rgbgreen[idx] -
                     (out->hvwt[(idx - v1) >> 1] * out->dgrb1[(idx - v1) >> 1] +
                      (1.0f - out->hvwt[(idx + 1) >> 1]) * out->dgrb1[(idx + 1) >> 1] +
                      (1.0f - out->hvwt[(idx - 1) >> 1]) * out->dgrb1[(idx - 1) >> 1] +
                      out->hvwt[(idx + v1) >> 1] * out->dgrb1[(idx + v1) >> 1]) *
                         temp);
            }
            if (cc1 & 1)
            {
                out->red[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb0[idx >> 1]);
                out->blue[idx] = 65535.0f * (out->rgbgreen[idx] - out->dgrb1[idx >> 1]);
            }
        }
    }

    for (int rr = 16; rr < rr1 - 16; ++rr)
    {
        for (int cc = 16, idx = rr * kTileSize + cc; cc < cc1 - 16; ++cc, ++idx)
        {
            out->green[idx] = 65535.0f * out->rgbgreen[idx];
        }
    }
}

__global__ void k_tile_load(const uint16_t * raw,
                            float * cfa,
                            float * rgbgreen,
                            int width,
                            int height,
                            int top,
                            int left,
                            int rr1,
                            int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr >= rr1 || cc >= cc1) return;

    const int row = reflect_index(top + rr, height);
    const int col = reflect_index(left + cc, width);
    const int idx = rr * kTileSize + cc;
    const float normalized =
        static_cast<float>(raw[static_cast<std::size_t>(row) * width + col]) /
        65535.0f;
    cfa[idx] = normalized;
    rgbgreen[idx] = (fc_rggb(rr, cc) == 1) ? normalized : 0.0f;
}

__global__ void k_gradients(const float * cfa,
                            float * dirwts0,
                            float * dirwts1,
                            float * delhvsqsum,
                            int rr1,
                            int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 2 || rr >= rr1 - 2 || cc < 2 || cc >= cc1 - 2) return;

    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int idx = rr * kTileSize + cc;
    const float delh = fabsf(cfa[idx + 1] - cfa[idx - 1]);
    const float delv = fabsf(cfa[idx + v1] - cfa[idx - v1]);
    dirwts0[idx] =
        kEps + fabsf(cfa[idx + v2] - cfa[idx]) +
        fabsf(cfa[idx] - cfa[idx - v2]) + delv;
    dirwts1[idx] =
        kEps + fabsf(cfa[idx + 2] - cfa[idx]) +
        fabsf(cfa[idx] - cfa[idx - 2]) + delh;
    delhvsqsum[idx] = sqr_f(delh) + sqr_f(delv);
}

__global__ void k_diagonal_precursors(const float * cfa,
                                      float * delp,
                                      float * delm,
                                      float * dgrbsq1p,
                                      float * dgrbsq1m,
                                      int rr1,
                                      int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 6 || rr >= rr1 - 6 || cc < 6 || cc >= cc1 - 6) return;
    if (((cc - 6) & 1) != 0) return;

    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int idx = rr * kTileSize + cc;
    const int halfIdx = idx >> 1;
    if ((fc_rggb(rr, 2) & 1) == 0)
    {
        delp[halfIdx] = fabsf(cfa[idx + p1] - cfa[idx - p1]);
        delm[halfIdx] = fabsf(cfa[idx + m1] - cfa[idx - m1]);
        dgrbsq1p[halfIdx] =
            sqr_f(cfa[idx + 1] - cfa[idx + 1 - p1]) +
            sqr_f(cfa[idx + 1] - cfa[idx + 1 + p1]);
        dgrbsq1m[halfIdx] =
            sqr_f(cfa[idx + 1] - cfa[idx + 1 - m1]) +
            sqr_f(cfa[idx + 1] - cfa[idx + 1 + m1]);
    }
    else
    {
        dgrbsq1p[halfIdx] =
            sqr_f(cfa[idx] - cfa[idx - p1]) +
            sqr_f(cfa[idx] - cfa[idx + p1]);
        dgrbsq1m[halfIdx] =
            sqr_f(cfa[idx] - cfa[idx - m1]) +
            sqr_f(cfa[idx] - cfa[idx + m1]);
        delp[halfIdx] = fabsf(cfa[idx + 1 + p1] - cfa[idx + 1 - p1]);
        delm[halfIdx] = fabsf(cfa[idx + 1 + m1] - cfa[idx + 1 - m1]);
    }
}

__global__ void k_green_interpolation(const float * cfa,
                                      const float * dirwts0,
                                      const float * dirwts1,
                                      float * vcd,
                                      float * hcd,
                                      float * vcdalt,
                                      float * hcdalt,
                                      float * dgintv,
                                      float * dginth,
                                      int rr1,
                                      int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 4 || rr >= rr1 - 4 || cc < 4 || cc >= cc1 - 4) return;

    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int idx = rr * kTileSize + cc;

    const float cru =
        cfa[idx - v1] * (dirwts0[idx - v2] + dirwts0[idx]) /
        (dirwts0[idx - v2] * (kEps + cfa[idx]) +
         dirwts0[idx] * (kEps + cfa[idx - v2]));
    const float crd =
        cfa[idx + v1] * (dirwts0[idx + v2] + dirwts0[idx]) /
        (dirwts0[idx + v2] * (kEps + cfa[idx]) +
         dirwts0[idx] * (kEps + cfa[idx + v2]));
    const float crl =
        cfa[idx - 1] * (dirwts1[idx - 2] + dirwts1[idx]) /
        (dirwts1[idx - 2] * (kEps + cfa[idx]) +
         dirwts1[idx] * (kEps + cfa[idx - 2]));
    const float crr =
        cfa[idx + 1] * (dirwts1[idx + 2] + dirwts1[idx]) /
        (dirwts1[idx + 2] * (kEps + cfa[idx]) +
         dirwts1[idx] * (kEps + cfa[idx + 2]));

    const float guha = cfa[idx - v1] + xdiv2f_probe(cfa[idx] - cfa[idx - v2]);
    const float gdha = cfa[idx + v1] + xdiv2f_probe(cfa[idx] - cfa[idx + v2]);
    const float glha = cfa[idx - 1] + xdiv2f_probe(cfa[idx] - cfa[idx - 2]);
    const float grha = cfa[idx + 1] + xdiv2f_probe(cfa[idx] - cfa[idx + 2]);

    float guar = (fabsf(1.0f - cru) < kAdaptiveRatioThreshold) ? cfa[idx] * cru : guha;
    float gdar = (fabsf(1.0f - crd) < kAdaptiveRatioThreshold) ? cfa[idx] * crd : gdha;
    float glar = (fabsf(1.0f - crl) < kAdaptiveRatioThreshold) ? cfa[idx] * crl : glha;
    float grar = (fabsf(1.0f - crr) < kAdaptiveRatioThreshold) ? cfa[idx] * crr : grha;

    const float hwt = dirwts1[idx - 1] / (dirwts1[idx - 1] + dirwts1[idx + 1]);
    const float vwt = dirwts0[idx - v1] / (dirwts0[idx + v1] + dirwts0[idx - v1]);
    const float gintvha = vwt * gdha + (1.0f - vwt) * guha;
    const float ginthha = hwt * grha + (1.0f - hwt) * glha;
    const float vinterp = vwt * gdar + (1.0f - vwt) * guar;
    const float hinterp = hwt * grar + (1.0f - hwt) * glar;

    if ((fc_rggb(rr, cc) & 1) != 0)
    {
        vcd[idx] = cfa[idx] - vinterp;
        hcd[idx] = cfa[idx] - hinterp;
        vcdalt[idx] = cfa[idx] - gintvha;
        hcdalt[idx] = cfa[idx] - ginthha;
    }
    else
    {
        vcd[idx] = vinterp - cfa[idx];
        hcd[idx] = hinterp - cfa[idx];
        vcdalt[idx] = gintvha - cfa[idx];
        hcdalt[idx] = ginthha - cfa[idx];
    }

    if (cfa[idx] > kClipPoint8 || gintvha > kClipPoint8 || ginthha > kClipPoint8)
    {
        guar = guha;
        gdar = gdha;
        glar = glha;
        grar = grha;
        vcd[idx] = vcdalt[idx];
        hcd[idx] = hcdalt[idx];
    }

    dgintv[idx] = fminf(sqr_f(guha - gdha), sqr_f(guar - gdar));
    dginth[idx] = fminf(sqr_f(glha - grha), sqr_f(glar - grar));
}

__device__ void variance_selection_update_pixel(const float * cfa,
                                                float * vcd,
                                                float * hcd,
                                                const float * vcdalt,
                                                const float * hcdalt,
                                                float * cddiffsq,
                                                int rr,
                                                int cc)
{
    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int idx = rr * kTileSize + cc;
    const float hcdvar =
        3.0f * (sqr_f(hcd[idx - 2]) +
                sqr_f(hcd[idx]) +
                sqr_f(hcd[idx + 2])) -
        sqr_f(hcd[idx - 2] + hcd[idx] + hcd[idx + 2]);
    const float hcdaltvar =
        3.0f * (sqr_f(hcdalt[idx - 2]) +
                sqr_f(hcdalt[idx]) +
                sqr_f(hcdalt[idx + 2])) -
        sqr_f(hcdalt[idx - 2] + hcdalt[idx] + hcdalt[idx + 2]);
    const float vcdvar =
        3.0f * (sqr_f(vcd[idx - v2]) +
                sqr_f(vcd[idx]) +
                sqr_f(vcd[idx + v2])) -
        sqr_f(vcd[idx - v2] + vcd[idx] + vcd[idx + v2]);
    const float vcdaltvar =
        3.0f * (sqr_f(vcdalt[idx - v2]) +
                sqr_f(vcdalt[idx]) +
                sqr_f(vcdalt[idx + v2])) -
        sqr_f(vcdalt[idx - v2] + vcdalt[idx] + vcdalt[idx + v2]);

    if (hcdaltvar < hcdvar) hcd[idx] = hcdalt[idx];
    if (vcdaltvar < vcdvar) vcd[idx] = vcdalt[idx];

    if ((fc_rggb(rr, cc) & 1) != 0)
    {
        const float ginth = -hcd[idx] + cfa[idx];
        const float gintv = -vcd[idx] + cfa[idx];
        if (hcd[idx] > 0.0f)
        {
            const float bounded = -ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) + cfa[idx];
            if (3.0f * hcd[idx] > (ginth + cfa[idx]))
            {
                hcd[idx] = bounded;
            }
            else
            {
                const float hwt = 1.0f - 3.0f * hcd[idx] / (kEps + ginth + cfa[idx]);
                hcd[idx] = hwt * hcd[idx] + (1.0f - hwt) * bounded;
            }
        }
        if (vcd[idx] > 0.0f)
        {
            const float bounded = -ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) + cfa[idx];
            if (3.0f * vcd[idx] > (gintv + cfa[idx]))
            {
                vcd[idx] = bounded;
            }
            else
            {
                const float vwt = 1.0f - 3.0f * vcd[idx] / (kEps + gintv + cfa[idx]);
                vcd[idx] = vwt * vcd[idx] + (1.0f - vwt) * bounded;
            }
        }
        if (ginth > kClipPoint)
        {
            hcd[idx] = -ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) + cfa[idx];
        }
        if (gintv > kClipPoint)
        {
            vcd[idx] = -ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) + cfa[idx];
        }
    }
    else
    {
        const float ginth = hcd[idx] + cfa[idx];
        const float gintv = vcd[idx] + cfa[idx];
        if (hcd[idx] < 0.0f)
        {
            const float bounded = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) - cfa[idx];
            if (3.0f * hcd[idx] < -(ginth + cfa[idx]))
            {
                hcd[idx] = bounded;
            }
            else
            {
                const float hwt = 1.0f + 3.0f * hcd[idx] / (kEps + ginth + cfa[idx]);
                hcd[idx] = hwt * hcd[idx] + (1.0f - hwt) * bounded;
            }
        }
        if (vcd[idx] < 0.0f)
        {
            const float bounded = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) - cfa[idx];
            if (3.0f * vcd[idx] < -(gintv + cfa[idx]))
            {
                vcd[idx] = bounded;
            }
            else
            {
                const float vwt = 1.0f + 3.0f * vcd[idx] / (kEps + gintv + cfa[idx]);
                vcd[idx] = vwt * vcd[idx] + (1.0f - vwt) * bounded;
            }
        }
        if (ginth > kClipPoint)
        {
            hcd[idx] = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) - cfa[idx];
        }
        if (gintv > kClipPoint)
        {
            vcd[idx] = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) - cfa[idx];
        }
        cddiffsq[idx] = sqr_f(vcd[idx] - hcd[idx]);
    }
}

// Preserve the serial pass's same-parity left/up dependencies while exposing
// each anti-diagonal inside a tile to parallel threads.
__global__ void k_variance_selection_wavefront_parity_blocks(float * cfa,
                                                             float * vcd,
                                                             float * hcd,
                                                             const float * vcdalt,
                                                             const float * hcdalt,
                                                             float * cddiffsq,
                                                             int rr1,
                                                             int cc1)
{
    if (blockIdx.y != 0 || blockIdx.x >= 4) return;

    const int tid = threadIdx.y * blockDim.x + threadIdx.x;
    const int threadCount = blockDim.x * blockDim.y;
    const int rrParity = static_cast<int>(blockIdx.x >> 1);
    const int ccParity = static_cast<int>(blockIdx.x & 1);

    int rrStart = 4;
    if ((rrStart & 1) != rrParity) ++rrStart;
    if (rrStart >= rr1 - 4) return;
    const int rrCount = ((rr1 - 5 - rrStart) / 2) + 1;

    int ccStart = 4;
    if ((ccStart & 1) != ccParity) ++ccStart;
    if (ccStart >= cc1 - 4) return;
    const int ccCount = ((cc1 - 5 - ccStart) / 2) + 1;
    const int maxDiag = rrCount + ccCount - 2;

    for (int diag = 0; diag <= maxDiag; ++diag)
    {
        const int prMinCandidate = diag - (ccCount - 1);
        const int prMin = prMinCandidate > 0 ? prMinCandidate : 0;
        const int prMax = diag < rrCount ? diag : rrCount - 1;
        const int diagCount = prMax - prMin + 1;
        for (int i = tid; i < diagCount; i += threadCount)
        {
            const int pr = prMin + i;
            const int pc = diag - pr;
            const int rr = rrStart + 2 * pr;
            const int cc = ccStart + 2 * pc;
            variance_selection_update_pixel(cfa,
                                            vcd,
                                            hcd,
                                            vcdalt,
                                            hcdalt,
                                            cddiffsq,
                                            rr,
                                            cc);
        }
        __syncthreads();
    }
}

__device__ float variance_selection_finish_h_candidate(const float * cfa,
                                                       float h,
                                                       int rr,
                                                       int cc)
{
    const int idx = rr * kTileSize + cc;
    if ((fc_rggb(rr, cc) & 1) != 0)
    {
        const float ginth = -h + cfa[idx];
        if (h > 0.0f)
        {
            const float bounded = -ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) + cfa[idx];
            if (3.0f * h > (ginth + cfa[idx]))
            {
                h = bounded;
            }
            else
            {
                const float hwt = 1.0f - 3.0f * h / (kEps + ginth + cfa[idx]);
                h = hwt * h + (1.0f - hwt) * bounded;
            }
        }
        if (ginth > kClipPoint)
        {
            h = -ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) + cfa[idx];
        }
    }
    else
    {
        const float ginth = h + cfa[idx];
        if (h < 0.0f)
        {
            const float bounded = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) - cfa[idx];
            if (3.0f * h < -(ginth + cfa[idx]))
            {
                h = bounded;
            }
            else
            {
                const float hwt = 1.0f + 3.0f * h / (kEps + ginth + cfa[idx]);
                h = hwt * h + (1.0f - hwt) * bounded;
            }
        }
        if (ginth > kClipPoint)
        {
            h = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]) - cfa[idx];
        }
    }
    return h;
}

__device__ float variance_selection_finish_v_candidate(const float * cfa,
                                                       float v,
                                                       int rr,
                                                       int cc)
{
    const int v1 = kTileSize;
    const int idx = rr * kTileSize + cc;
    if ((fc_rggb(rr, cc) & 1) != 0)
    {
        const float gintv = -v + cfa[idx];
        if (v > 0.0f)
        {
            const float bounded = -ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) + cfa[idx];
            if (3.0f * v > (gintv + cfa[idx]))
            {
                v = bounded;
            }
            else
            {
                const float vwt = 1.0f - 3.0f * v / (kEps + gintv + cfa[idx]);
                v = vwt * v + (1.0f - vwt) * bounded;
            }
        }
        if (gintv > kClipPoint)
        {
            v = -ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) + cfa[idx];
        }
    }
    else
    {
        const float gintv = v + cfa[idx];
        if (v < 0.0f)
        {
            const float bounded = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) - cfa[idx];
            if (3.0f * v < -(gintv + cfa[idx]))
            {
                v = bounded;
            }
            else
            {
                const float vwt = 1.0f + 3.0f * v / (kEps + gintv + cfa[idx]);
                v = vwt * v + (1.0f - vwt) * bounded;
            }
        }
        if (gintv > kClipPoint)
        {
            v = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]) - cfa[idx];
        }
    }
    return v;
}

__device__ unsigned char binary_map_apply(unsigned char map, unsigned int input)
{
    return static_cast<unsigned char>((map >> (input & 1u)) & 1u);
}

__device__ unsigned char binary_map_compose(unsigned char before,
                                            unsigned char after)
{
    const unsigned char out0 =
        binary_map_apply(after, binary_map_apply(before, 0));
    const unsigned char out1 =
        binary_map_apply(after, binary_map_apply(before, 1));
    return static_cast<unsigned char>(out0 | (out1 << 1));
}

__global__ void k_variance_selection_candidate_finals(const float * cfa,
                                                      const float * vcd,
                                                      const float * hcd,
                                                      const float * vcdalt,
                                                      const float * hcdalt,
                                                      float * h0,
                                                      float * h1,
                                                      float * v0,
                                                      float * v1,
                                                      int rr1,
                                                      int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 4 || rr >= rr1 - 4 || cc < 4 || cc >= cc1 - 4) return;

    const int idx = rr * kTileSize + cc;
    h0[idx] = variance_selection_finish_h_candidate(cfa, hcd[idx], rr, cc);
    h1[idx] = variance_selection_finish_h_candidate(cfa, hcdalt[idx], rr, cc);
    v0[idx] = variance_selection_finish_v_candidate(cfa, vcd[idx], rr, cc);
    v1[idx] = variance_selection_finish_v_candidate(cfa, vcdalt[idx], rr, cc);
}

__device__ unsigned char variance_h_choice_map(const float * hcd,
                                               const float * hcdalt,
                                               float left,
                                               int idx)
{
    const float hcdvar =
        3.0f * (sqr_f(left) +
                sqr_f(hcd[idx]) +
                sqr_f(hcd[idx + 2])) -
        sqr_f(left + hcd[idx] + hcd[idx + 2]);
    const float hcdaltvar =
        3.0f * (sqr_f(hcdalt[idx - 2]) +
                sqr_f(hcdalt[idx]) +
                sqr_f(hcdalt[idx + 2])) -
        sqr_f(hcdalt[idx - 2] + hcdalt[idx] + hcdalt[idx + 2]);
    return hcdaltvar < hcdvar ? 1 : 0;
}

__device__ unsigned char variance_v_choice_map(const float * vcd,
                                               const float * vcdalt,
                                               float up,
                                               int idx)
{
    const int v2 = 2 * kTileSize;
    const float vcdvar =
        3.0f * (sqr_f(up) +
                sqr_f(vcd[idx]) +
                sqr_f(vcd[idx + v2])) -
        sqr_f(up + vcd[idx] + vcd[idx + v2]);
    const float vcdaltvar =
        3.0f * (sqr_f(vcdalt[idx - v2]) +
                sqr_f(vcdalt[idx]) +
                sqr_f(vcdalt[idx + v2])) -
        sqr_f(vcdalt[idx - v2] + vcdalt[idx] + vcdalt[idx + v2]);
    return vcdaltvar < vcdvar ? 1 : 0;
}

__global__ void k_variance_selection_build_maps(const float * vcd,
                                                const float * hcd,
                                                const float * vcdalt,
                                                const float * hcdalt,
                                                const float * h0,
                                                const float * h1,
                                                const float * v0,
                                                const float * v1,
                                                unsigned char * hMap,
                                                unsigned char * vMap,
                                                int rr1,
                                                int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 4 || rr >= rr1 - 4 || cc < 4 || cc >= cc1 - 4) return;

    const int idx = rr * kTileSize + cc;

    float left0 = hcd[idx - 2];
    float left1 = left0;
    if (cc - 2 >= 4)
    {
        left0 = h0[idx - 2];
        left1 = h1[idx - 2];
    }
    const unsigned char hOut0 = variance_h_choice_map(hcd, hcdalt, left0, idx);
    const unsigned char hOut1 = variance_h_choice_map(hcd, hcdalt, left1, idx);
    hMap[idx] = static_cast<unsigned char>(hOut0 | (hOut1 << 1));

    const int v2 = 2 * kTileSize;
    float up0 = vcd[idx - v2];
    float up1 = up0;
    if (rr - 2 >= 4)
    {
        up0 = v0[idx - v2];
        up1 = v1[idx - v2];
    }
    const unsigned char vOut0 = variance_v_choice_map(vcd, vcdalt, up0, idx);
    const unsigned char vOut1 = variance_v_choice_map(vcd, vcdalt, up1, idx);
    vMap[idx] = static_cast<unsigned char>(vOut0 | (vOut1 << 1));
}

__global__ void k_variance_selection_h_row_prefix(const unsigned char * hMap,
                                                  unsigned char * hChoice,
                                                  int rr1,
                                                  int cc1)
{
    const int rr = blockIdx.x;
    const int ccParity = blockIdx.y;
    if (rr < 4 || rr >= rr1 - 4 || ccParity > 1) return;

    const int tid = threadIdx.y * blockDim.x + threadIdx.x;
    __shared__ unsigned char maps[kTileHalf];

    int ccStart = 4;
    if ((ccStart & 1) != ccParity) ++ccStart;
    if (ccStart >= cc1 - 4) return;
    const int count = ((cc1 - 5 - ccStart) / 2) + 1;

    if (tid < kTileHalf)
    {
        if (tid < count)
        {
            const int idx = rr * kTileSize + ccStart + 2 * tid;
            maps[tid] = hMap[idx];
        }
        else
        {
            maps[tid] = 2;
        }
    }
    __syncthreads();

    for (int offset = 1; offset < kTileHalf; offset <<= 1)
    {
        unsigned char combined = 2;
        if (tid < kTileHalf)
        {
            const unsigned char mine = maps[tid];
            const unsigned char before = (tid >= offset) ? maps[tid - offset] : 2;
            combined = binary_map_compose(before, mine);
        }
        __syncthreads();
        if (tid < kTileHalf && tid >= offset)
        {
            maps[tid] = combined;
        }
        __syncthreads();
    }

    if (tid < count)
    {
        const int idx = rr * kTileSize + ccStart + 2 * tid;
        hChoice[idx] = binary_map_apply(maps[tid], 0);
    }
}

__global__ void k_variance_selection_v_column_prefix(const unsigned char * vMap,
                                                     unsigned char * vChoice,
                                                     int rr1,
                                                     int cc1)
{
    const int cc = blockIdx.x;
    const int rrParity = blockIdx.y;
    if (cc < 4 || cc >= cc1 - 4 || rrParity > 1) return;

    const int tid = threadIdx.y * blockDim.x + threadIdx.x;
    __shared__ unsigned char maps[kTileHalf];

    int rrStart = 4;
    if ((rrStart & 1) != rrParity) ++rrStart;
    if (rrStart >= rr1 - 4) return;
    const int count = ((rr1 - 5 - rrStart) / 2) + 1;

    if (tid < kTileHalf)
    {
        if (tid < count)
        {
            const int idx = (rrStart + 2 * tid) * kTileSize + cc;
            maps[tid] = vMap[idx];
        }
        else
        {
            maps[tid] = 2;
        }
    }
    __syncthreads();

    for (int offset = 1; offset < kTileHalf; offset <<= 1)
    {
        unsigned char combined = 2;
        if (tid < kTileHalf)
        {
            const unsigned char mine = maps[tid];
            const unsigned char before = (tid >= offset) ? maps[tid - offset] : 2;
            combined = binary_map_compose(before, mine);
        }
        __syncthreads();
        if (tid < kTileHalf && tid >= offset)
        {
            maps[tid] = combined;
        }
        __syncthreads();
    }

    if (tid < count)
    {
        const int idx = (rrStart + 2 * tid) * kTileSize + cc;
        vChoice[idx] = binary_map_apply(maps[tid], 0);
    }
}

__global__ void k_variance_selection_finalize_prefix(float * vcd,
                                                     float * hcd,
                                                     float * cddiffsq,
                                                     const float * h0,
                                                     const float * h1,
                                                     const float * v0,
                                                     const float * v1,
                                                     const unsigned char * hChoice,
                                                     const unsigned char * vChoice,
                                                     int rr1,
                                                     int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 4 || rr >= rr1 - 4 || cc < 4 || cc >= cc1 - 4) return;

    const int idx = rr * kTileSize + cc;
    hcd[idx] = hChoice[idx] ? h1[idx] : h0[idx];
    vcd[idx] = vChoice[idx] ? v1[idx] : v0[idx];

    if ((fc_rggb(rr, cc) & 1) == 0)
    {
        cddiffsq[idx] = sqr_f(vcd[idx] - hcd[idx]);
    }
}

__global__ void k_hvwt_adaptive_weights(const float * dirwts0,
                                        const float * dirwts1,
                                        const float * vcd,
                                        const float * hcd,
                                        const float * dgintv,
                                        const float * dginth,
                                        float * hvwt,
                                        int rr1,
                                        int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 6 || rr >= rr1 - 6) return;

    const int ccStart = 6 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 6 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int v3 = 3 * kTileSize;
    const float epssq = kEps * kEps;
    const int idx = rr * kTileSize + cc;

    const float uave = vcd[idx] + vcd[idx - v1] + vcd[idx - v2] + vcd[idx - v3];
    const float dave = vcd[idx] + vcd[idx + v1] + vcd[idx + v2] + vcd[idx + v3];
    const float lave = hcd[idx] + hcd[idx - 1] + hcd[idx - 2] + hcd[idx - 3];
    const float rave = hcd[idx] + hcd[idx + 1] + hcd[idx + 2] + hcd[idx + 3];

    const float dgrbvvaru =
        sqr_f(vcd[idx] - uave) +
        sqr_f(vcd[idx - v1] - uave) +
        sqr_f(vcd[idx - v2] - uave) +
        sqr_f(vcd[idx - v3] - uave);
    const float dgrbvvard =
        sqr_f(vcd[idx] - dave) +
        sqr_f(vcd[idx + v1] - dave) +
        sqr_f(vcd[idx + v2] - dave) +
        sqr_f(vcd[idx + v3] - dave);
    const float dgrbhvarl =
        sqr_f(hcd[idx] - lave) +
        sqr_f(hcd[idx - 1] - lave) +
        sqr_f(hcd[idx - 2] - lave) +
        sqr_f(hcd[idx - 3] - lave);
    const float dgrbhvarr =
        sqr_f(hcd[idx] - rave) +
        sqr_f(hcd[idx + 1] - rave) +
        sqr_f(hcd[idx + 2] - rave) +
        sqr_f(hcd[idx + 3] - rave);

    const float hwt = dirwts1[idx - 1] / (dirwts1[idx - 1] + dirwts1[idx + 1]);
    const float vwt = dirwts0[idx - v1] / (dirwts0[idx + v1] + dirwts0[idx - v1]);

    const float vcdvar = epssq + vwt * dgrbvvard + (1.0f - vwt) * dgrbvvaru;
    const float hcdvar = epssq + hwt * dgrbhvarr + (1.0f - hwt) * dgrbhvarl;

    const float dgrbvvaru1 = dgintv[idx] + dgintv[idx - v1] + dgintv[idx - v2];
    const float dgrbvvard1 = dgintv[idx] + dgintv[idx + v1] + dgintv[idx + v2];
    const float dgrbhvarl1 = dginth[idx] + dginth[idx - 1] + dginth[idx - 2];
    const float dgrbhvarr1 = dginth[idx] + dginth[idx + 1] + dginth[idx + 2];

    const float vcdvar1 = epssq + vwt * dgrbvvard1 + (1.0f - vwt) * dgrbvvaru1;
    const float hcdvar1 = epssq + hwt * dgrbhvarr1 + (1.0f - hwt) * dgrbhvarl1;

    const float varwt = hcdvar / (vcdvar + hcdvar);
    const float diffwt = hcdvar1 / (vcdvar1 + hcdvar1);
    if ((0.5f - varwt) * (0.5f - diffwt) > 0.0f &&
        fabsf(0.5f - diffwt) < fabsf(0.5f - varwt))
    {
        hvwt[idx >> 1] = varwt;
    }
    else
    {
        hvwt[idx >> 1] = diffwt;
    }
}

__global__ void k_nyquist_test(const float * cddiffsq,
                               const float * delhvsqsum,
                               unsigned char * nyquist,
                               int rr1,
                               int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 6 || rr >= rr1 - 6) return;

    const int ccStart = 6 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 6 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int p2 = -2 * kTileSize + 2;
    const int m2 = 2 * kTileSize + 2;
    const int idx = rr * kTileSize + cc;

    float nyqtest =
        gauss_odd(0) * cddiffsq[idx] +
        gauss_odd(1) * (cddiffsq[idx - m1] + cddiffsq[idx + p1] +
                        cddiffsq[idx - p1] + cddiffsq[idx + m1]) +
        gauss_odd(2) * (cddiffsq[idx - v2] + cddiffsq[idx - 2] +
                        cddiffsq[idx + 2] + cddiffsq[idx + v2]) +
        gauss_odd(3) * (cddiffsq[idx - m2] + cddiffsq[idx + p2] +
                        cddiffsq[idx - p2] + cddiffsq[idx + m2]);

    nyqtest -=
        kNyquistThreshold *
        (gauss_grad(0) * delhvsqsum[idx] +
         gauss_grad(1) * (delhvsqsum[idx - v1] + delhvsqsum[idx + 1] +
                          delhvsqsum[idx - 1] + delhvsqsum[idx + v1]) +
         gauss_grad(2) * (delhvsqsum[idx - m1] + delhvsqsum[idx + p1] +
                          delhvsqsum[idx - p1] + delhvsqsum[idx + m1]) +
         gauss_grad(3) * (delhvsqsum[idx - v2] + delhvsqsum[idx - 2] +
                          delhvsqsum[idx + 2] + delhvsqsum[idx + v2]) +
         gauss_grad(4) * (delhvsqsum[idx - 2 * kTileSize - 1] +
                          delhvsqsum[idx - 2 * kTileSize + 1] +
                          delhvsqsum[idx - kTileSize - 2] +
                          delhvsqsum[idx - kTileSize + 2] +
                          delhvsqsum[idx + kTileSize - 2] +
                          delhvsqsum[idx + kTileSize + 2] +
                          delhvsqsum[idx + 2 * kTileSize - 1] +
                          delhvsqsum[idx + 2 * kTileSize + 1]) +
         gauss_grad(5) * (delhvsqsum[idx - m2] + delhvsqsum[idx + p2] +
                          delhvsqsum[idx - p2] + delhvsqsum[idx + m2]));

    if (nyqtest > 0.0f)
    {
        nyquist[idx >> 1] = 1;
    }
}

__device__ unsigned char nyquist_refine_result(unsigned int nyquisttemp,
                                               unsigned char current)
{
    if (nyquisttemp > 4) return 1;
    if (nyquisttemp < 4) return 0;
    return current ? 1 : 0;
}

__device__ unsigned char nyquist_refine_apply_map(unsigned char map,
                                                  unsigned int input)
{
    return static_cast<unsigned char>((map >> (input & 1u)) & 1u);
}

__device__ unsigned char nyquist_refine_compose_maps(unsigned char before,
                                                     unsigned char after)
{
    const unsigned char out0 =
        nyquist_refine_apply_map(after, nyquist_refine_apply_map(before, 0));
    const unsigned char out1 =
        nyquist_refine_apply_map(after, nyquist_refine_apply_map(before, 1));
    return static_cast<unsigned char>(out0 | (out1 << 1));
}

__device__ unsigned char nyquist_refine_build_map(unsigned int sumNoLeft,
                                                  unsigned char current)
{
    const unsigned char out0 = nyquist_refine_result(sumNoLeft, current);
    const unsigned char out1 = nyquist_refine_result(sumNoLeft + 1u, current);
    return static_cast<unsigned char>(out0 | (out1 << 1));
}

__global__ void k_nyquist_refine_row_prefix(unsigned char * nyquist, int rr1, int cc1)
{
    if (blockIdx.x != 0 || blockIdx.y != 0) return;

    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int tid = threadIdx.y * blockDim.x + threadIdx.x;

    __shared__ unsigned char maps[kTileHalf];
    __shared__ unsigned char rowBoundaryLeft;

    // Preserve the scalar row-major pass: rows are finalized in order, while
    // each row is scanned as a Boolean recurrence over the same-row left bit.
    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
        if (ccStart >= cc1 - 8) continue;
        const int count = ((cc1 - 9 - ccStart) / 2) + 1;

        if (tid == 0)
        {
            const int idxStart = rr * kTileSize + ccStart;
            rowBoundaryLeft = nyquist[(idxStart - 2) >> 1];
        }

        if (tid < kTileHalf)
        {
            if (tid < count)
            {
                const int cc = ccStart + 2 * tid;
                const int idx = rr * kTileSize + cc;
                const unsigned int sumNoLeft =
                    nyquist[(idx - v2) >> 1] +
                    nyquist[(idx - m1) >> 1] +
                    nyquist[(idx + p1) >> 1] +
                    nyquist[idx >> 1] +
                    nyquist[(idx + 2) >> 1] +
                    nyquist[(idx - p1) >> 1] +
                    nyquist[(idx + m1) >> 1] +
                    nyquist[(idx + v2) >> 1];
                maps[tid] = nyquist_refine_build_map(sumNoLeft, nyquist[idx >> 1]);
            }
            else
            {
                maps[tid] = 2;
            }
        }
        __syncthreads();

        for (int offset = 1; offset < kTileHalf; offset <<= 1)
        {
            unsigned char combined = 2;
            if (tid < kTileHalf)
            {
                const unsigned char mine = maps[tid];
                const unsigned char before = (tid >= offset) ? maps[tid - offset] : 2;
                combined = nyquist_refine_compose_maps(before, mine);
            }
            __syncthreads();
            if (tid < kTileHalf && tid >= offset)
            {
                maps[tid] = combined;
            }
            __syncthreads();
        }

        if (tid < count)
        {
            const int cc = ccStart + 2 * tid;
            const int idx = rr * kTileSize + cc;
            nyquist[idx >> 1] = nyquist_refine_apply_map(maps[tid], rowBoundaryLeft);
        }
        __syncthreads();
    }
}

__global__ void k_nyquist_area_interpolation(const float * cfa,
                                             const unsigned char * nyquist,
                                             float * hvwt,
                                             int rr1,
                                             int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 8 || rr >= rr1 - 8) return;

    const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 8 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const float epssq = kEps * kEps;
    const int idx = rr * kTileSize + cc;
    if (!nyquist[idx >> 1]) return;

    float sumh = 0.0f;
    float sumv = 0.0f;
    float sumsqh = 0.0f;
    float sumsqv = 0.0f;
    float areawt = 0.0f;
    for (int i = -6; i < 7; i += 2)
    {
        for (int j = -6; j < 7; j += 2)
        {
            const int idx1 = (rr + i) * kTileSize + cc + j;
            if (nyquist[idx1 >> 1])
            {
                sumh += cfa[idx1] - xdiv2f_probe(cfa[idx1 - 1] + cfa[idx1 + 1]);
                sumv += cfa[idx1] - xdiv2f_probe(cfa[idx1 - v1] + cfa[idx1 + v1]);
                sumsqh +=
                    xdiv2f_probe(sqr_f(cfa[idx1] - cfa[idx1 - 1]) +
                                 sqr_f(cfa[idx1] - cfa[idx1 + 1]));
                sumsqv +=
                    xdiv2f_probe(sqr_f(cfa[idx1] - cfa[idx1 - v1]) +
                                 sqr_f(cfa[idx1] - cfa[idx1 + v1]));
                areawt += 1.0f;
            }
        }
    }

    const float hcdvar = epssq + fabsf(areawt * sumsqh - sumh * sumh);
    const float vcdvar = epssq + fabsf(areawt * sumsqv - sumv * sumv);
    hvwt[idx >> 1] = hcdvar / (vcdvar + hcdvar);
}

__global__ void k_green_plane_hvwt_refinement_row_parallel(float * hvwt,
                                                           int rr1,
                                                           int cc1)
{
    const int tid = threadIdx.x;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;

    for (int rr = 8; rr < rr1 - 8; ++rr)
    {
        const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
        for (int cc = ccStart + 2 * tid, idx = rr * kTileSize + cc;
             cc < cc1 - 8;
             cc += 2 * blockDim.x, idx += 2 * blockDim.x)
        {
            const float hvwtalt =
                xdivf_probe(hvwt[(idx - m1) >> 1] +
                            hvwt[(idx + p1) >> 1] +
                            hvwt[(idx - p1) >> 1] +
                            hvwt[(idx + m1) >> 1],
                            2);
            if (fabsf(0.5f - hvwt[idx >> 1]) < fabsf(0.5f - hvwtalt))
            {
                hvwt[idx >> 1] = hvwtalt;
            }
        }
        __syncthreads();
    }
}

__global__ void k_green_plane_assembly_grid(const float * cfa,
                                            const float * vcd,
                                            const float * hcd,
                                            const unsigned char * nyquist,
                                            const float * hvwt,
                                            float * dgrb0,
                                            float * rgbgreen,
                                            float * dgrb2h,
                                            float * dgrb2v,
                                            int rr1,
                                            int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 8 || rr >= rr1 - 8) return;

    const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 8 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const int idx = rr * kTileSize + cc;
    const int halfIdx = idx >> 1;

    dgrb0[halfIdx] = hcd[idx] * (1.0f - hvwt[halfIdx]) + vcd[idx] * hvwt[halfIdx];
    rgbgreen[idx] = cfa[idx] + dgrb0[halfIdx];

    if (nyquist[halfIdx])
    {
        dgrb2h[halfIdx] =
            sqr_f(rgbgreen[idx] - xdiv2f_probe(rgbgreen[idx - 1] + rgbgreen[idx + 1]));
        dgrb2v[halfIdx] =
            sqr_f(rgbgreen[idx] - xdiv2f_probe(rgbgreen[idx - v1] + rgbgreen[idx + v1]));
    }
    else
    {
        dgrb2h[halfIdx] = 0.0f;
        dgrb2v[halfIdx] = 0.0f;
    }
}

__global__ void k_nyquist_green_refinement(const float * cfa,
                                           const float * vcd,
                                           const float * hcd,
                                           const unsigned char * nyquist,
                                           const float * dgrb2h,
                                           const float * dgrb2v,
                                           float * dgrb0,
                                           float * rgbgreen,
                                           int rr1,
                                           int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 8 || rr >= rr1 - 8) return;

    const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 8 || ((cc - ccStart) & 1) != 0) return;

    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int p2 = -2 * kTileSize + 2;
    const int m2 = 2 * kTileSize + 2;
    const float epssq = kEps * kEps;
    const int idx = rr * kTileSize + cc;
    if (!nyquist[idx >> 1]) return;

    const float gvarh =
        epssq +
        (gquinc(0) * dgrb2h[idx >> 1] +
         gquinc(1) * (dgrb2h[(idx - m1) >> 1] + dgrb2h[(idx + p1) >> 1] +
                      dgrb2h[(idx - p1) >> 1] + dgrb2h[(idx + m1) >> 1]) +
         gquinc(2) * (dgrb2h[(idx - v2) >> 1] + dgrb2h[(idx - 2) >> 1] +
                      dgrb2h[(idx + 2) >> 1] + dgrb2h[(idx + v2) >> 1]) +
         gquinc(3) * (dgrb2h[(idx - m2) >> 1] + dgrb2h[(idx + p2) >> 1] +
                      dgrb2h[(idx - p2) >> 1] + dgrb2h[(idx + m2) >> 1]));
    const float gvarv =
        epssq +
        (gquinc(0) * dgrb2v[idx >> 1] +
         gquinc(1) * (dgrb2v[(idx - m1) >> 1] + dgrb2v[(idx + p1) >> 1] +
                      dgrb2v[(idx - p1) >> 1] + dgrb2v[(idx + m1) >> 1]) +
         gquinc(2) * (dgrb2v[(idx - v2) >> 1] + dgrb2v[(idx - 2) >> 1] +
                      dgrb2v[(idx + 2) >> 1] + dgrb2v[(idx + v2) >> 1]) +
         gquinc(3) * (dgrb2v[(idx - m2) >> 1] + dgrb2v[(idx + p2) >> 1] +
                      dgrb2v[(idx - p2) >> 1] + dgrb2v[(idx + m2) >> 1]));

    dgrb0[idx >> 1] = (hcd[idx] * gvarv + vcd[idx] * gvarh) / (gvarv + gvarh);
    rgbgreen[idx] = cfa[idx] + dgrb0[idx >> 1];
}

__global__ void k_diagonal_rb_interpolation(const float * cfa,
                                            const float * delp,
                                            const float * delm,
                                            const float * dgrbsq1p,
                                            const float * dgrbsq1m,
                                            float * rbm,
                                            float * rbp,
                                            float * pmwt,
                                            int rr1,
                                            int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 8 || rr >= rr1 - 8) return;

    const int ccStart = 8 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 8 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int p2 = -2 * kTileSize + 2;
    const int m2 = 2 * kTileSize + 2;
    const float epssq = kEps * kEps;
    const int idx = rr * kTileSize + cc;
    const int halfIdx = idx >> 1;

    const float crse = xmul2f_probe(cfa[idx + m1]) / (kEps + cfa[idx] + cfa[idx + m2]);
    const float crnw = xmul2f_probe(cfa[idx - m1]) / (kEps + cfa[idx] + cfa[idx - m2]);
    const float crne = xmul2f_probe(cfa[idx + p1]) / (kEps + cfa[idx] + cfa[idx + p2]);
    const float crsw = xmul2f_probe(cfa[idx - p1]) / (kEps + cfa[idx] + cfa[idx - p2]);

    const float rbse =
        (fabsf(1.0f - crse) < kAdaptiveRatioThreshold)
            ? cfa[idx] * crse
            : cfa[idx + m1] + xdiv2f_probe(cfa[idx] - cfa[idx + m2]);
    const float rbnw =
        (fabsf(1.0f - crnw) < kAdaptiveRatioThreshold)
            ? cfa[idx] * crnw
            : cfa[idx - m1] + xdiv2f_probe(cfa[idx] - cfa[idx - m2]);
    const float rbne =
        (fabsf(1.0f - crne) < kAdaptiveRatioThreshold)
            ? cfa[idx] * crne
            : cfa[idx + p1] + xdiv2f_probe(cfa[idx] - cfa[idx + p2]);
    const float rbsw =
        (fabsf(1.0f - crsw) < kAdaptiveRatioThreshold)
            ? cfa[idx] * crsw
            : cfa[idx - p1] + xdiv2f_probe(cfa[idx] - cfa[idx - p2]);

    const float wtse = kEps + delm[halfIdx] + delm[(idx + m1) >> 1] + delm[(idx + m2) >> 1];
    const float wtnw = kEps + delm[halfIdx] + delm[(idx - m1) >> 1] + delm[(idx - m2) >> 1];
    const float wtne = kEps + delp[halfIdx] + delp[(idx + p1) >> 1] + delp[(idx + p2) >> 1];
    const float wtsw = kEps + delp[halfIdx] + delp[(idx - p1) >> 1] + delp[(idx - p2) >> 1];

    rbm[halfIdx] = (wtse * rbnw + wtnw * rbse) / (wtse + wtnw);
    rbp[halfIdx] = (wtne * rbsw + wtsw * rbne) / (wtne + wtsw);

    const float rbvarm =
        epssq +
        (gauss_seven(0) * (dgrbsq1m[(idx - v1) >> 1] + dgrbsq1m[(idx - 1) >> 1] +
                           dgrbsq1m[(idx + 1) >> 1] + dgrbsq1m[(idx + v1) >> 1]) +
         gauss_seven(1) * (dgrbsq1m[(idx - v2 - 1) >> 1] +
                           dgrbsq1m[(idx - v2 + 1) >> 1] +
                           dgrbsq1m[(idx - 2 - v1) >> 1] +
                           dgrbsq1m[(idx + 2 - v1) >> 1] +
                           dgrbsq1m[(idx - 2 + v1) >> 1] +
                           dgrbsq1m[(idx + 2 + v1) >> 1] +
                           dgrbsq1m[(idx + v2 - 1) >> 1] +
                           dgrbsq1m[(idx + v2 + 1) >> 1]));
    const float rbvarp =
        epssq +
        (gauss_seven(0) * (dgrbsq1p[(idx - v1) >> 1] + dgrbsq1p[(idx - 1) >> 1] +
                           dgrbsq1p[(idx + 1) >> 1] + dgrbsq1p[(idx + v1) >> 1]) +
         gauss_seven(1) * (dgrbsq1p[(idx - v2 - 1) >> 1] +
                           dgrbsq1p[(idx - v2 + 1) >> 1] +
                           dgrbsq1p[(idx - 2 - v1) >> 1] +
                           dgrbsq1p[(idx + 2 - v1) >> 1] +
                           dgrbsq1p[(idx - 2 + v1) >> 1] +
                           dgrbsq1p[(idx + 2 + v1) >> 1] +
                           dgrbsq1p[(idx + v2 - 1) >> 1] +
                           dgrbsq1p[(idx + v2 + 1) >> 1]));
    pmwt[halfIdx] = rbvarm / (rbvarp + rbvarm);

    if (rbp[halfIdx] < cfa[idx])
    {
        const float bounded = ulim_f(rbp[halfIdx], cfa[idx - p1], cfa[idx + p1]);
        if (xmul2f_probe(rbp[halfIdx]) < cfa[idx])
        {
            rbp[halfIdx] = bounded;
        }
        else
        {
            const float pwt = xmul2f_probe(cfa[idx] - rbp[halfIdx]) /
                              (kEps + rbp[halfIdx] + cfa[idx]);
            rbp[halfIdx] = pwt * rbp[halfIdx] + (1.0f - pwt) * bounded;
        }
    }
    if (rbm[halfIdx] < cfa[idx])
    {
        const float bounded = ulim_f(rbm[halfIdx], cfa[idx - m1], cfa[idx + m1]);
        if (xmul2f_probe(rbm[halfIdx]) < cfa[idx])
        {
            rbm[halfIdx] = bounded;
        }
        else
        {
            const float mwt = xmul2f_probe(cfa[idx] - rbm[halfIdx]) /
                              (kEps + rbm[halfIdx] + cfa[idx]);
            rbm[halfIdx] = mwt * rbm[halfIdx] + (1.0f - mwt) * bounded;
        }
    }

    if (rbp[halfIdx] > kClipPoint)
    {
        rbp[halfIdx] = ulim_f(rbp[halfIdx], cfa[idx - p1], cfa[idx + p1]);
    }
    if (rbm[halfIdx] > kClipPoint)
    {
        rbm[halfIdx] = ulim_f(rbm[halfIdx], cfa[idx - m1], cfa[idx + m1]);
    }
}

__global__ void k_pmwt_refinement_row_parallel(float * pmwt,
                                               float * pmwtalt,
                                               int rr1,
                                               int cc1)
{
    if (blockIdx.x != 0) return;

    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    for (int rr = 10; rr < rr1 - 10; ++rr)
    {
        const int ccStart = 10 + (fc_rggb(rr, 2) & 1);
        for (int cc = ccStart + 2 * threadIdx.x, idx = rr * kTileSize + cc;
             cc < cc1 - 10;
             cc += 2 * blockDim.x, idx += 2 * blockDim.x)
        {
            const int halfIdx = idx >> 1;
            pmwtalt[halfIdx] =
                xdivf_probe(pmwt[(idx - m1) >> 1] +
                                pmwt[(idx + p1) >> 1] +
                                pmwt[(idx - p1) >> 1] +
                                pmwt[(idx + m1) >> 1],
                            2);
            if (fabsf(0.5f - pmwt[halfIdx]) < fabsf(0.5f - pmwtalt[halfIdx]))
            {
                pmwt[halfIdx] = pmwtalt[halfIdx];
            }
        }
        __syncthreads();
    }
}

__global__ void k_rbint_assembly_grid(const float * cfa,
                                      const float * rbm,
                                      const float * rbp,
                                      const float * pmwt,
                                      float * rbint,
                                      int rr1,
                                      int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 10 || rr >= rr1 - 10) return;

    const int ccStart = 10 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 10 || ((cc - ccStart) & 1) != 0) return;

    const int idx = rr * kTileSize + cc;
    const int halfIdx = idx >> 1;
    rbint[halfIdx] =
        xdiv2f_probe(cfa[idx] +
                     rbm[halfIdx] * (1.0f - pmwt[halfIdx]) +
                     rbp[halfIdx] * pmwt[halfIdx]);
}

__global__ void k_diagonal_green_correction(const float * cfa,
                                            const float * dirwts0,
                                            const float * dirwts1,
                                            const float * hvwt,
                                            const float * pmwt,
                                            const float * rbint,
                                            float * dgrb0,
                                            float * rgbgreen,
                                            int rr1,
                                            int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 12 || rr >= rr1 - 12) return;

    const int ccStart = 12 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 12 || ((cc - ccStart) & 1) != 0) return;

    const int v1 = kTileSize;
    const int idx = rr * kTileSize + cc;
    const int halfIdx = idx >> 1;
    if (fabsf(0.5f - pmwt[halfIdx]) < fabsf(0.5f - hvwt[halfIdx])) return;

    const float cruDenom = kEps + rbint[halfIdx] + rbint[halfIdx - v1];
    const float crdDenom = kEps + rbint[halfIdx] + rbint[halfIdx + v1];
    const float crlDenom = kEps + rbint[halfIdx] + rbint[halfIdx - 1];
    const float crrDenom = kEps + rbint[halfIdx] + rbint[halfIdx + 1];
    const float cru = double_twice_div_probe(cfa[idx - v1], cruDenom);
    const float crd = double_twice_div_probe(cfa[idx + v1], crdDenom);
    const float crl = double_twice_div_probe(cfa[idx - 1], crlDenom);
    const float crr = double_twice_div_probe(cfa[idx + 1], crrDenom);

    const float gu =
        (fabsf(1.0f - cru) < kAdaptiveRatioThreshold)
            ? rbint[halfIdx] * cru
            : cfa[idx - v1] + xdiv2f_probe(rbint[halfIdx] - rbint[halfIdx - v1]);
    const float gd =
        (fabsf(1.0f - crd) < kAdaptiveRatioThreshold)
            ? rbint[halfIdx] * crd
            : cfa[idx + v1] + xdiv2f_probe(rbint[halfIdx] - rbint[halfIdx + v1]);
    const float gl =
        (fabsf(1.0f - crl) < kAdaptiveRatioThreshold)
            ? rbint[halfIdx] * crl
            : cfa[idx - 1] + xdiv2f_probe(rbint[halfIdx] - rbint[halfIdx - 1]);
    const float gr =
        (fabsf(1.0f - crr) < kAdaptiveRatioThreshold)
            ? rbint[halfIdx] * crr
            : cfa[idx + 1] + xdiv2f_probe(rbint[halfIdx] - rbint[halfIdx + 1]);

    float gintv =
        (dirwts0[idx - v1] * gd + dirwts0[idx + v1] * gu) /
        (dirwts0[idx + v1] + dirwts0[idx - v1]);
    float ginth =
        (dirwts1[idx - 1] * gr + dirwts1[idx + 1] * gl) /
        (dirwts1[idx - 1] + dirwts1[idx + 1]);

    if (gintv < rbint[halfIdx])
    {
        if (2.0f * gintv < rbint[halfIdx])
        {
            gintv = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]);
        }
        else
        {
            const float diff = rbint[halfIdx] - gintv;
            const float denom = kEps + gintv + rbint[halfIdx];
            const float vwt = double_twice_div_probe(diff, denom);
            gintv = vwt * gintv + (1.0f - vwt) * ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]);
        }
    }
    if (ginth < rbint[halfIdx])
    {
        if (2.0f * ginth < rbint[halfIdx])
        {
            ginth = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]);
        }
        else
        {
            const float diff = rbint[halfIdx] - ginth;
            const float denom = kEps + ginth + rbint[halfIdx];
            const float hwt = double_twice_div_probe(diff, denom);
            ginth = hwt * ginth + (1.0f - hwt) * ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]);
        }
    }

    if (ginth > kClipPoint)
    {
        ginth = ulim_f(ginth, cfa[idx - 1], cfa[idx + 1]);
    }
    if (gintv > kClipPoint)
    {
        gintv = ulim_f(gintv, cfa[idx - v1], cfa[idx + v1]);
    }

    rgbgreen[idx] = ginth * (1.0f - hvwt[halfIdx]) + gintv * hvwt[halfIdx];
    dgrb0[halfIdx] = rgbgreen[idx] - cfa[idx];
}

__global__ void k_chrominance_coset_split(float * dgrb0,
                                          float * dgrb1,
                                          int rr1,
                                          int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;

    int ex = 0;
    int ey = 0;
    if (fc_rggb(0, 0) == 1)
    {
        if (fc_rggb(0, 1) == 0)
        {
            ey = 0;
            ex = 1;
        }
        else
        {
            ey = 1;
            ex = 0;
        }
    }
    else
    {
        if (fc_rggb(0, 0) == 0)
        {
            ey = 0;
            ex = 0;
        }
        else
        {
            ey = 1;
            ex = 1;
        }
    }

    if (rr < 13 - ey || rr >= rr1 - 12 || ((rr - (13 - ey)) & 1) != 0) return;
    if (cc < 13 - ex || cc >= cc1 - 12 || ((cc - (13 - ex)) & 1) != 0) return;

    const int halfIdx = (rr * kTileSize + cc) >> 1;
    dgrb1[halfIdx] = dgrb0[halfIdx];
    dgrb0[halfIdx] = 0.0f;
}

__global__ void k_fancy_chrominance_interpolation(float * dgrb0,
                                                  float * dgrb1,
                                                  int rr1,
                                                  int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 14 || rr >= rr1 - 14) return;

    const int ccStart = 14 + (fc_rggb(rr, 2) & 1);
    if (cc < ccStart || cc >= cc1 - 14 || ((cc - ccStart) & 1) != 0) return;

    const int v2 = 2 * kTileSize;
    const int p1 = -kTileSize + 1;
    const int m1 = kTileSize + 1;
    const int p3 = -3 * kTileSize + 3;
    const int m3 = 3 * kTileSize + 3;
    const int idx = rr * kTileSize + cc;
    const int channel = 1 - fc_rggb(rr, cc) / 2;
    float * dgrb = (channel == 0) ? dgrb0 : dgrb1;

    const float wtnw =
        1.0f /
        (kEps +
         fabsf(dgrb[(idx - m1) >> 1] - dgrb[(idx + m1) >> 1]) +
         fabsf(dgrb[(idx - m1) >> 1] - dgrb[(idx - m3) >> 1]) +
         fabsf(dgrb[(idx + m1) >> 1] - dgrb[(idx - m3) >> 1]));
    const float wtne =
        1.0f /
        (kEps +
         fabsf(dgrb[(idx + p1) >> 1] - dgrb[(idx - p1) >> 1]) +
         fabsf(dgrb[(idx + p1) >> 1] - dgrb[(idx + p3) >> 1]) +
         fabsf(dgrb[(idx - p1) >> 1] - dgrb[(idx + p3) >> 1]));
    const float wtsw =
        1.0f /
        (kEps +
         fabsf(dgrb[(idx - p1) >> 1] - dgrb[(idx + p1) >> 1]) +
         fabsf(dgrb[(idx - p1) >> 1] - dgrb[(idx + m3) >> 1]) +
         fabsf(dgrb[(idx + p1) >> 1] - dgrb[(idx - p3) >> 1]));
    const float wtse =
        1.0f /
        (kEps +
         fabsf(dgrb[(idx + m1) >> 1] - dgrb[(idx - m1) >> 1]) +
         fabsf(dgrb[(idx + m1) >> 1] - dgrb[(idx - p3) >> 1]) +
         fabsf(dgrb[(idx - m1) >> 1] - dgrb[(idx + m3) >> 1]));

    dgrb[idx >> 1] =
        (wtnw * (1.325f * dgrb[(idx - m1) >> 1] -
                 0.175f * dgrb[(idx - m3) >> 1] -
                 0.075f * dgrb[(idx - m1 - 2) >> 1] -
                 0.075f * dgrb[(idx - m1 - v2) >> 1]) +
         wtne * (1.325f * dgrb[(idx + p1) >> 1] -
                 0.175f * dgrb[(idx + p3) >> 1] -
                 0.075f * dgrb[(idx + p1 + 2) >> 1] -
                 0.075f * dgrb[(idx + p1 + v2) >> 1]) +
         wtsw * (1.325f * dgrb[(idx - p1) >> 1] -
                 0.175f * dgrb[(idx - p3) >> 1] -
                 0.075f * dgrb[(idx - p1 - 2) >> 1] -
                 0.075f * dgrb[(idx - p1 - v2) >> 1]) +
         wtse * (1.325f * dgrb[(idx + m1) >> 1] -
                 0.175f * dgrb[(idx + m3) >> 1] -
                 0.075f * dgrb[(idx + m1 + 2) >> 1] -
                 0.075f * dgrb[(idx + m1 + v2) >> 1])) /
        (wtnw + wtne + wtsw + wtse);
}

__global__ void k_final_output_planes(const float * rgbgreen,
                                      const float * hvwt,
                                      const float * dgrb0,
                                      const float * dgrb1,
                                      float * red,
                                      float * green,
                                      float * blue,
                                      int rr1,
                                      int cc1)
{
    const int cc = blockIdx.x * blockDim.x + threadIdx.x;
    const int rr = blockIdx.y * blockDim.y + threadIdx.y;
    if (rr < 16 || rr >= rr1 - 16 || cc < 16 || cc >= cc1 - 16) return;

    const int idx = rr * kTileSize + cc;
    if ((fc_rggb(rr, cc) & 1) != 0)
    {
        const float temp =
            1.0f /
            (hvwt[(idx - kTileSize) >> 1] +
             (1.0f - hvwt[(idx + 1) >> 1]) +
             (1.0f - hvwt[(idx - 1) >> 1]) +
             hvwt[(idx + kTileSize) >> 1]);
        red[idx] =
            65535.0f *
            (rgbgreen[idx] -
             (hvwt[(idx - kTileSize) >> 1] * dgrb0[(idx - kTileSize) >> 1] +
              (1.0f - hvwt[(idx + 1) >> 1]) * dgrb0[(idx + 1) >> 1] +
              (1.0f - hvwt[(idx - 1) >> 1]) * dgrb0[(idx - 1) >> 1] +
              hvwt[(idx + kTileSize) >> 1] * dgrb0[(idx + kTileSize) >> 1]) *
                 temp);
        blue[idx] =
            65535.0f *
            (rgbgreen[idx] -
             (hvwt[(idx - kTileSize) >> 1] * dgrb1[(idx - kTileSize) >> 1] +
              (1.0f - hvwt[(idx + 1) >> 1]) * dgrb1[(idx + 1) >> 1] +
              (1.0f - hvwt[(idx - 1) >> 1]) * dgrb1[(idx - 1) >> 1] +
              hvwt[(idx + kTileSize) >> 1] * dgrb1[(idx + kTileSize) >> 1]) *
                 temp);
    }
    else
    {
        red[idx] = 65535.0f * (rgbgreen[idx] - dgrb0[idx >> 1]);
        blue[idx] = 65535.0f * (rgbgreen[idx] - dgrb1[idx >> 1]);
    }

    green[idx] = 65535.0f * rgbgreen[idx];
}

#ifdef CUDA_AMAZE_DEBAYER_STAGE_PROBE_LIBRARY
struct CudaStageProbeError : public std::runtime_error
{
    explicit CudaStageProbeError(const std::string & message)
        : std::runtime_error(message)
    {
    }
};
#endif

void check_cuda(cudaError_t rc, const char * call, const char * file, int line)
{
    if (rc == cudaSuccess) return;
#ifdef CUDA_AMAZE_DEBAYER_STAGE_PROBE_LIBRARY
    throw CudaStageProbeError(
        std::string("CUDA error ") + call + " @ " + file + ":" +
        std::to_string(line) + ": " + cudaGetErrorString(rc));
#else
    std::fprintf(stderr,
                 "CUDA error %s @ %s:%d: %s\n",
                 call,
                 file,
                 line,
                 cudaGetErrorString(rc));
    std::exit(10);
#endif
}

#define CK(call) check_cuda((call), #call, __FILE__, __LINE__)

void launch_variance_selection_prefix(const DeviceBuffers & d,
                                      int rr1,
                                      int cc1,
                                      cudaStream_t stream = 0,
                                      bool checkLaunch = true)
{
    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);
    const dim3 prefixGrid(kTileSize, 2);

#define CHECK_VARIANCE_PREFIX_LAUNCH() \
    do { \
        if (checkLaunch) CK(cudaGetLastError()); \
    } while (0)

    IGPU_LT_LAUNCH("k_variance_selection_candidate_finals", grid, block, stream,
                   k_variance_selection_candidate_finals<<<grid, block, 0, stream>>>(d.cfa,
                                                                                     d.vcd,
                                                                                     d.hcd,
                                                                                     d.vcdalt,
                                                                                     d.hcdalt,
                                                                                     d.variance_h0,
                                                                                     d.variance_h1,
                                                                                     d.variance_v0,
                                                                                     d.variance_v1,
                                                                                     rr1,
                                                                                     cc1));
    CHECK_VARIANCE_PREFIX_LAUNCH();
    IGPU_LT_LAUNCH("k_variance_selection_build_maps", grid, block, stream,
                   k_variance_selection_build_maps<<<grid, block, 0, stream>>>(d.vcd,
                                                                              d.hcd,
                                                                              d.vcdalt,
                                                                              d.hcdalt,
                                                                              d.variance_h0,
                                                                              d.variance_h1,
                                                                              d.variance_v0,
                                                                              d.variance_v1,
                                                                              d.variance_h_map,
                                                                              d.variance_v_map,
                                                                              rr1,
                                                                              cc1));
    CHECK_VARIANCE_PREFIX_LAUNCH();
    IGPU_LT_LAUNCH("k_variance_selection_h_row_prefix", prefixGrid, kVariancePrefixThreads, stream,
                   k_variance_selection_h_row_prefix<<<prefixGrid, kVariancePrefixThreads, 0, stream>>>(
                       d.variance_h_map,
                       d.variance_h_choice,
                       rr1,
                       cc1));
    CHECK_VARIANCE_PREFIX_LAUNCH();
    IGPU_LT_LAUNCH("k_variance_selection_v_column_prefix", prefixGrid, kVariancePrefixThreads, stream,
                   k_variance_selection_v_column_prefix<<<prefixGrid, kVariancePrefixThreads, 0, stream>>>(
                       d.variance_v_map,
                       d.variance_v_choice,
                       rr1,
                       cc1));
    CHECK_VARIANCE_PREFIX_LAUNCH();
    IGPU_LT_LAUNCH("k_variance_selection_finalize_prefix", grid, block, stream,
                   k_variance_selection_finalize_prefix<<<grid, block, 0, stream>>>(d.vcd,
                                                                                   d.hcd,
                                                                                   d.cddiffsq,
                                                                                   d.variance_h0,
                                                                                   d.variance_h1,
                                                                                   d.variance_v0,
                                                                                   d.variance_v1,
                                                                                   d.variance_h_choice,
                                                                                   d.variance_v_choice,
                                                                                   rr1,
                                                                                   cc1));
    CHECK_VARIANCE_PREFIX_LAUNCH();

#undef CHECK_VARIANCE_PREFIX_LAUNCH
}

void add_elapsed_ms(cudaEvent_t start, cudaEvent_t stop, double * total)
{
    CK(cudaEventRecord(stop));
    CK(IGPU_LT_SYNC_CALL("probe:elapsed_sync", cudaEventSynchronize(stop)));
    float elapsed = 0.0f;
    CK(cudaEventElapsedTime(&elapsed, start, stop));
    *total += static_cast<double>(elapsed);
}

void launch_green_plane_assembly(const dim3 & grid,
                                 const dim3 & block,
                                 const float * cfa,
                                 const float * vcd,
                                 const float * hcd,
                                 const unsigned char * nyquist,
                                 float * hvwt,
                                 float * dgrb0,
                                 float * rgbgreen,
                                 float * dgrb2h,
                                 float * dgrb2v,
                                 int rr1,
                                 int cc1,
                                 cudaStream_t stream = 0,
                                 bool checkLaunch = true)
{
    IGPU_LT_LAUNCH("k_green_plane_hvwt_refinement_row_parallel", 1, kGreenPlaneThreads, stream,
                   k_green_plane_hvwt_refinement_row_parallel<<<1, kGreenPlaneThreads, 0, stream>>>(
                       hvwt,
                       rr1,
                       cc1));
    if (checkLaunch) CK(cudaGetLastError());

    IGPU_LT_LAUNCH("k_green_plane_assembly_grid", grid, block, stream,
                   k_green_plane_assembly_grid<<<grid, block, 0, stream>>>(cfa,
                                                                           vcd,
                                                                           hcd,
                                                                           nyquist,
                                                                           hvwt,
                                                                           dgrb0,
                                                                           rgbgreen,
                                                                           dgrb2h,
                                                                           dgrb2v,
                                                                           rr1,
                                                                           cc1));
    if (checkLaunch) CK(cudaGetLastError());
}

void launch_pmwt_refinement_and_rbint(const dim3 & grid,
                                      const dim3 & block,
                                      const float * cfa,
                                      const float * rbm,
                                      const float * rbp,
                                      float * pmwt,
                                      float * pmwtalt,
                                      float * rbint,
                                      int rr1,
                                      int cc1,
                                      cudaStream_t stream = 0,
                                      bool checkLaunch = true)
{
    IGPU_LT_LAUNCH("k_pmwt_refinement_row_parallel", 1, kPmwtRowThreads, stream,
                   k_pmwt_refinement_row_parallel<<<1, kPmwtRowThreads, 0, stream>>>(pmwt,
                                                                                     pmwtalt,
                                                                                     rr1,
                                                                                     cc1));
    if (checkLaunch) CK(cudaGetLastError());

    IGPU_LT_LAUNCH("k_rbint_assembly_grid", grid, block, stream,
                   k_rbint_assembly_grid<<<grid, block, 0, stream>>>(cfa,
                                                                     rbm,
                                                                     rbp,
                                                                     pmwt,
                                                                     rbint,
                                                                     rr1,
                                                                     cc1));
    if (checkLaunch) CK(cudaGetLastError());
}

void allocate_device(DeviceBuffers * d, std::size_t rawCount)
{
    CK(cudaMalloc(&d->raw, rawCount * sizeof(uint16_t)));
    CK(cudaMalloc(&d->cfa, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->rgbgreen, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->red, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->green, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->blue, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dirwts0, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dirwts1, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->delhvsqsum, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->delp, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->delm, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dgrbsq1p, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dgrbsq1m, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->vcd, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->hcd, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->vcdalt, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->hcdalt, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->variance_h0, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->variance_h1, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->variance_v0, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->variance_v1, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->variance_h_map, kTileSamples * sizeof(unsigned char)));
    CK(cudaMalloc(&d->variance_v_map, kTileSamples * sizeof(unsigned char)));
    CK(cudaMalloc(&d->variance_h_choice, kTileSamples * sizeof(unsigned char)));
    CK(cudaMalloc(&d->variance_v_choice, kTileSamples * sizeof(unsigned char)));
    CK(cudaMalloc(&d->dgintv, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dginth, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->cddiffsq, kTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->hvwt, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->nyquist, kHalfTileSamples * sizeof(unsigned char)));
    CK(cudaMalloc(&d->dgrb0, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dgrb1, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dgrb2h, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->dgrb2v, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->rbm, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->rbp, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->pmwt, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->pmwtalt, kHalfTileSamples * sizeof(float)));
    CK(cudaMalloc(&d->rbint, kHalfTileSamples * sizeof(float)));
}

void free_device(DeviceBuffers * d)
{
    cudaFree(d->raw);
    cudaFree(d->cfa);
    cudaFree(d->rgbgreen);
    cudaFree(d->red);
    cudaFree(d->green);
    cudaFree(d->blue);
    cudaFree(d->dirwts0);
    cudaFree(d->dirwts1);
    cudaFree(d->delhvsqsum);
    cudaFree(d->delp);
    cudaFree(d->delm);
    cudaFree(d->dgrbsq1p);
    cudaFree(d->dgrbsq1m);
    cudaFree(d->vcd);
    cudaFree(d->hcd);
    cudaFree(d->vcdalt);
    cudaFree(d->hcdalt);
    cudaFree(d->variance_h0);
    cudaFree(d->variance_h1);
    cudaFree(d->variance_v0);
    cudaFree(d->variance_v1);
    cudaFree(d->variance_h_map);
    cudaFree(d->variance_v_map);
    cudaFree(d->variance_h_choice);
    cudaFree(d->variance_v_choice);
    cudaFree(d->dgintv);
    cudaFree(d->dginth);
    cudaFree(d->cddiffsq);
    cudaFree(d->hvwt);
    cudaFree(d->nyquist);
    cudaFree(d->dgrb0);
    cudaFree(d->dgrb1);
    cudaFree(d->dgrb2h);
    cudaFree(d->dgrb2v);
    cudaFree(d->rbm);
    cudaFree(d->rbp);
    cudaFree(d->pmwt);
    cudaFree(d->pmwtalt);
    cudaFree(d->rbint);
}

void clear_device_stages(const DeviceBuffers & d)
{
#ifndef CUDA_AMAZE_ENABLE_DEVICE_CLEAR
    (void)d;
    return;
#else
    CK(cudaMemset(d.cfa, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.rgbgreen, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.red, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.green, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.blue, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.dirwts0, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.dirwts1, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.delhvsqsum, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.delp, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.delm, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.dgrbsq1p, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.dgrbsq1m, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.vcd, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.hcd, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.vcdalt, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.hcdalt, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.variance_h0, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.variance_h1, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.variance_v0, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.variance_v1, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.variance_h_map, 0, kTileSamples * sizeof(unsigned char)));
    CK(cudaMemset(d.variance_v_map, 0, kTileSamples * sizeof(unsigned char)));
    CK(cudaMemset(d.variance_h_choice, 0, kTileSamples * sizeof(unsigned char)));
    CK(cudaMemset(d.variance_v_choice, 0, kTileSamples * sizeof(unsigned char)));
    CK(cudaMemset(d.dgintv, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.dginth, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.cddiffsq, 0, kTileSamples * sizeof(float)));
    CK(cudaMemset(d.hvwt, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.nyquist, 0, kHalfTileSamples * sizeof(unsigned char)));
    CK(cudaMemset(d.dgrb0, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.dgrb1, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.dgrb2h, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.dgrb2v, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.rbm, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.rbp, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.pmwt, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.pmwtalt, 0, kHalfTileSamples * sizeof(float)));
    CK(cudaMemset(d.rbint, 0, kHalfTileSamples * sizeof(float)));
#endif
}

void copy_device_to_host(const DeviceBuffers & d, StageBuffers * out)
{
    CK(cudaMemcpy(out->cfa.data(), d.cfa, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->rgbgreen.data(), d.rgbgreen, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->red.data(), d.red, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->green.data(), d.green, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->blue.data(), d.blue, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dirwts0.data(), d.dirwts0, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dirwts1.data(), d.dirwts1, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->delhvsqsum.data(), d.delhvsqsum, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->delp.data(), d.delp, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->delm.data(), d.delm, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrbsq1p.data(), d.dgrbsq1p, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrbsq1m.data(), d.dgrbsq1m, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->vcd.data(), d.vcd, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->hcd.data(), d.hcd, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->vcdalt.data(), d.vcdalt, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->hcdalt.data(), d.hcdalt, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgintv.data(), d.dgintv, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dginth.data(), d.dginth, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->cddiffsq.data(), d.cddiffsq, kTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->hvwt.data(), d.hvwt, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->nyquist.data(), d.nyquist, kHalfTileSamples * sizeof(unsigned char), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrb0.data(), d.dgrb0, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrb1.data(), d.dgrb1, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrb2h.data(), d.dgrb2h, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->dgrb2v.data(), d.dgrb2v, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->rbm.data(), d.rbm, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->rbp.data(), d.rbp, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->pmwt.data(), d.pmwt, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->pmwtalt.data(), d.pmwtalt, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(out->rbint.data(), d.rbint, kHalfTileSamples * sizeof(float), cudaMemcpyDeviceToHost));
}

uint32_t float_bits(float value)
{
    uint32_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

CompareStats compare_array(const std::vector<float> & cpu,
                           const std::vector<float> & gpu)
{
    CompareStats stats;
    for (std::size_t i = 0; i < cpu.size(); ++i)
    {
        const float diff = std::fabs(cpu[i] - gpu[i]);
        if (diff > stats.maxAbs)
        {
            stats.maxAbs = diff;
            stats.maxIndex = i;
        }
        if (diff > kTolerance)
        {
            ++stats.mismatches;
        }
        if (float_bits(cpu[i]) != float_bits(gpu[i]))
        {
            ++stats.bitMismatches;
        }
    }
    return stats;
}

ByteCompareStats compare_array(const std::vector<unsigned char> & cpu,
                               const std::vector<unsigned char> & gpu)
{
    ByteCompareStats stats;
    for (std::size_t i = 0; i < cpu.size(); ++i)
    {
        const int diff = std::abs(static_cast<int>(cpu[i]) - static_cast<int>(gpu[i]));
        if (diff > stats.maxAbs)
        {
            stats.maxAbs = diff;
            stats.maxIndex = i;
        }
        if (diff != 0)
        {
            ++stats.mismatches;
            ++stats.bitMismatches;
        }
    }
    return stats;
}

bool report_compare(const char * caseName,
                    const char * arrayName,
                    const std::vector<float> & cpu,
                    const std::vector<float> & gpu)
{
    const CompareStats stats = compare_array(cpu, gpu);
    std::cout << "  " << caseName << "." << arrayName
              << ": max_abs=" << std::setprecision(9) << stats.maxAbs
              << " mismatches_gt_" << kTolerance << "=" << stats.mismatches
              << " bit_mismatches=" << stats.bitMismatches
              << " max_index=" << stats.maxIndex
              << "\n";
    return stats.mismatches == 0;
}

bool report_compare(const char * caseName,
                    const char * arrayName,
                    const std::vector<unsigned char> & cpu,
                    const std::vector<unsigned char> & gpu)
{
    const ByteCompareStats stats = compare_array(cpu, gpu);
    std::cout << "  " << caseName << "." << arrayName
              << ": max_abs=" << stats.maxAbs
              << " mismatches=" << stats.mismatches
              << " bit_mismatches=" << stats.bitMismatches
              << " max_index=" << stats.maxIndex
              << "\n";
    return stats.mismatches == 0;
}

bool run_case(const CaseSpec & spec)
{
    const int bottom = imin_i(spec.top + kTileSize, spec.height + 16);
    const int right = imin_i(spec.left + kTileSize, spec.width + 16);
    const int rr1 = bottom - spec.top;
    const int cc1 = right - spec.left;
    const std::vector<uint16_t> raw =
        make_raw_pattern(spec.width, spec.height, 0xA5A50000u ^
                         static_cast<uint32_t>(spec.top * 131 + spec.left));

    StageBuffers cpu;
    StageBuffers gpu;
    cpu_stage_probe(raw, spec, &cpu);

    DeviceBuffers d;
    allocate_device(&d, raw.size());
    CK(cudaMemcpy(d.raw,
                  raw.data(),
                  raw.size() * sizeof(uint16_t),
                  cudaMemcpyHostToDevice));
    clear_device_stages(d);

    const dim3 block(16, 16);
    const dim3 grid((kTileSize + block.x - 1) / block.x,
                    (kTileSize + block.y - 1) / block.y);
    k_tile_load<<<grid, block>>>(d.raw,
                                 d.cfa,
                                 d.rgbgreen,
                                 spec.width,
                                 spec.height,
                                 spec.top,
                                 spec.left,
                                 rr1,
                                 cc1);
    CK(cudaGetLastError());
    k_gradients<<<grid, block>>>(d.cfa,
                                 d.dirwts0,
                                 d.dirwts1,
                                 d.delhvsqsum,
                                 rr1,
                                 cc1);
    CK(cudaGetLastError());
    k_diagonal_precursors<<<grid, block>>>(d.cfa,
                                           d.delp,
                                           d.delm,
                                           d.dgrbsq1p,
                                           d.dgrbsq1m,
                                           rr1,
                                           cc1);
    CK(cudaGetLastError());
    k_green_interpolation<<<grid, block>>>(d.cfa,
                                           d.dirwts0,
                                           d.dirwts1,
                                           d.vcd,
                                           d.hcd,
                                           d.vcdalt,
                                           d.hcdalt,
                                           d.dgintv,
                                           d.dginth,
                                           rr1,
                                           cc1);
    CK(cudaGetLastError());
    launch_variance_selection_prefix(d, rr1, cc1);
    k_hvwt_adaptive_weights<<<grid, block>>>(d.dirwts0,
                                             d.dirwts1,
                                             d.vcd,
                                             d.hcd,
                                             d.dgintv,
                                             d.dginth,
                                             d.hvwt,
                                             rr1,
                                             cc1);
    CK(cudaGetLastError());
    k_nyquist_test<<<grid, block>>>(d.cddiffsq,
                                    d.delhvsqsum,
                                    d.nyquist,
                                    rr1,
                                    cc1);
    CK(cudaGetLastError());
    k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads>>>(d.nyquist, rr1, cc1);
    CK(cudaGetLastError());
    k_nyquist_area_interpolation<<<grid, block>>>(d.cfa,
                                                  d.nyquist,
                                                  d.hvwt,
                                                  rr1,
                                                  cc1);
    CK(cudaGetLastError());
    launch_green_plane_assembly(grid,
                                block,
                                d.cfa,
                                d.vcd,
                                d.hcd,
                                d.nyquist,
                                d.hvwt,
                                d.dgrb0,
                                d.rgbgreen,
                                d.dgrb2h,
                                d.dgrb2v,
                                rr1,
                                cc1);
    k_nyquist_green_refinement<<<grid, block>>>(d.cfa,
                                                d.vcd,
                                                d.hcd,
                                                d.nyquist,
                                                d.dgrb2h,
                                                d.dgrb2v,
                                                d.dgrb0,
                                                d.rgbgreen,
                                                rr1,
                                                cc1);
    CK(cudaGetLastError());
    k_diagonal_rb_interpolation<<<grid, block>>>(d.cfa,
                                                 d.delp,
                                                 d.delm,
                                                 d.dgrbsq1p,
                                                 d.dgrbsq1m,
                                                 d.rbm,
                                                 d.rbp,
                                                 d.pmwt,
                                                 rr1,
                                                 cc1);
    CK(cudaGetLastError());
    launch_pmwt_refinement_and_rbint(grid,
                                     block,
                                     d.cfa,
                                     d.rbm,
                                     d.rbp,
                                     d.pmwt,
                                     d.pmwtalt,
                                     d.rbint,
                                     rr1,
                                     cc1);
    k_diagonal_green_correction<<<grid, block>>>(d.cfa,
                                                 d.dirwts0,
                                                 d.dirwts1,
                                                 d.hvwt,
                                                 d.pmwt,
                                                 d.rbint,
                                                 d.dgrb0,
                                                 d.rgbgreen,
                                                 rr1,
                                                 cc1);
    CK(cudaGetLastError());
    k_chrominance_coset_split<<<grid, block>>>(d.dgrb0,
                                               d.dgrb1,
                                               rr1,
                                               cc1);
    CK(cudaGetLastError());
    k_fancy_chrominance_interpolation<<<grid, block>>>(d.dgrb0,
                                                       d.dgrb1,
                                                       rr1,
                                                       cc1);
    CK(cudaGetLastError());
    k_final_output_planes<<<grid, block>>>(d.rgbgreen,
                                           d.hvwt,
                                           d.dgrb0,
                                           d.dgrb1,
                                           d.red,
                                           d.green,
                                           d.blue,
                                           rr1,
                                           cc1);
    CK(cudaGetLastError());
    CK(cudaDeviceSynchronize());

    copy_device_to_host(d, &gpu);
    free_device(&d);

    std::cout << "[case] " << spec.name
              << " image=" << spec.width << "x" << spec.height
              << " tile_top_left=(" << spec.top << "," << spec.left << ")"
              << " tile_extent=" << rr1 << "x" << cc1 << "\n";

    bool ok = true;
    ok = report_compare(spec.name, "cfa", cpu.cfa, gpu.cfa) && ok;
    ok = report_compare(spec.name, "rgbgreen", cpu.rgbgreen, gpu.rgbgreen) && ok;
    ok = report_compare(spec.name, "red", cpu.red, gpu.red) && ok;
    ok = report_compare(spec.name, "green", cpu.green, gpu.green) && ok;
    ok = report_compare(spec.name, "blue", cpu.blue, gpu.blue) && ok;
    ok = report_compare(spec.name, "dirwts0", cpu.dirwts0, gpu.dirwts0) && ok;
    ok = report_compare(spec.name, "dirwts1", cpu.dirwts1, gpu.dirwts1) && ok;
    ok = report_compare(spec.name, "delhvsqsum", cpu.delhvsqsum, gpu.delhvsqsum) && ok;
    ok = report_compare(spec.name, "delp", cpu.delp, gpu.delp) && ok;
    ok = report_compare(spec.name, "delm", cpu.delm, gpu.delm) && ok;
    ok = report_compare(spec.name, "dgrbsq1p", cpu.dgrbsq1p, gpu.dgrbsq1p) && ok;
    ok = report_compare(spec.name, "dgrbsq1m", cpu.dgrbsq1m, gpu.dgrbsq1m) && ok;
    ok = report_compare(spec.name, "vcd", cpu.vcd, gpu.vcd) && ok;
    ok = report_compare(spec.name, "hcd", cpu.hcd, gpu.hcd) && ok;
    ok = report_compare(spec.name, "vcdalt", cpu.vcdalt, gpu.vcdalt) && ok;
    ok = report_compare(spec.name, "hcdalt", cpu.hcdalt, gpu.hcdalt) && ok;
    ok = report_compare(spec.name, "dgintv", cpu.dgintv, gpu.dgintv) && ok;
    ok = report_compare(spec.name, "dginth", cpu.dginth, gpu.dginth) && ok;
    ok = report_compare(spec.name, "cddiffsq", cpu.cddiffsq, gpu.cddiffsq) && ok;
    ok = report_compare(spec.name, "hvwt", cpu.hvwt, gpu.hvwt) && ok;
    ok = report_compare(spec.name, "nyquist", cpu.nyquist, gpu.nyquist) && ok;
    ok = report_compare(spec.name, "dgrb0", cpu.dgrb0, gpu.dgrb0) && ok;
    ok = report_compare(spec.name, "dgrb1", cpu.dgrb1, gpu.dgrb1) && ok;
    ok = report_compare(spec.name, "dgrb2h", cpu.dgrb2h, gpu.dgrb2h) && ok;
    ok = report_compare(spec.name, "dgrb2v", cpu.dgrb2v, gpu.dgrb2v) && ok;
    ok = report_compare(spec.name, "rbm", cpu.rbm, gpu.rbm) && ok;
    ok = report_compare(spec.name, "rbp", cpu.rbp, gpu.rbp) && ok;
    ok = report_compare(spec.name, "pmwt", cpu.pmwt, gpu.pmwt) && ok;
    ok = report_compare(spec.name, "pmwtalt", cpu.pmwtalt, gpu.pmwtalt) && ok;
    ok = report_compare(spec.name, "rbint", cpu.rbint, gpu.rbint) && ok;
    return ok;
}

void run_timed_frame_once(const CaseSpec & spec,
                          const uint16_t * dRaw,
                          DeviceBuffers & d,
                          TimedStageTotals * totals)
{
    cudaEvent_t start = nullptr;
    cudaEvent_t stop = nullptr;
    CK(cudaEventCreate(&start));
    CK(cudaEventCreate(&stop));

#define RUN_TIMED_STAGE(memberName, ...) \
    do { \
        CK(cudaEventRecord(start)); \
        __VA_ARGS__; \
        CK(cudaGetLastError()); \
        add_elapsed_ms(start, stop, &totals->memberName); \
    } while (0)

#define RUN_TIMED_HOST_STAGE(memberName, ...) \
    do { \
        CK(cudaEventRecord(start)); \
        __VA_ARGS__; \
        add_elapsed_ms(start, stop, &totals->memberName); \
    } while (0)

    for (int top = -16; top < spec.height; top += kTileSize - 32)
    {
        for (int left = -16; left < spec.width; left += kTileSize - 32)
        {
            const int bottom = imin_i(top + kTileSize, spec.height + 16);
            const int right = imin_i(left + kTileSize, spec.width + 16);
            const int rr1 = bottom - top;
            const int cc1 = right - left;
            const dim3 block(16, 16);
            const dim3 grid((kTileSize + block.x - 1) / block.x,
                            (kTileSize + block.y - 1) / block.y);

            RUN_TIMED_HOST_STAGE(clearMs, clear_device_stages(d));
            RUN_TIMED_STAGE(loadMs,
                            k_tile_load<<<grid, block>>>(dRaw,
                                                         d.cfa,
                                                         d.rgbgreen,
                                                         spec.width,
                                                         spec.height,
                                                         top,
                                                         left,
                                                         rr1,
                                                         cc1));
            RUN_TIMED_STAGE(gradientsMs,
                            k_gradients<<<grid, block>>>(d.cfa,
                                                         d.dirwts0,
                                                         d.dirwts1,
                                                         d.delhvsqsum,
                                                         rr1,
                                                         cc1));
            RUN_TIMED_STAGE(diagonalPrecursorsMs,
                            k_diagonal_precursors<<<grid, block>>>(d.cfa,
                                                                   d.delp,
                                                                   d.delm,
                                                                   d.dgrbsq1p,
                                                                   d.dgrbsq1m,
                                                                   rr1,
                                                                   cc1));
            RUN_TIMED_STAGE(greenInterpolationMs,
                            k_green_interpolation<<<grid, block>>>(d.cfa,
                                                                   d.dirwts0,
                                                                   d.dirwts1,
                                                                   d.vcd,
                                                                   d.hcd,
                                                                   d.vcdalt,
                                                                   d.hcdalt,
                                                                   d.dgintv,
                                                                   d.dginth,
                                                                   rr1,
                                                                   cc1));
            RUN_TIMED_STAGE(varianceSelectionMs,
                            launch_variance_selection_prefix(d, rr1, cc1));
            RUN_TIMED_STAGE(hvwtAdaptiveMs,
                            k_hvwt_adaptive_weights<<<grid, block>>>(d.dirwts0,
                                                                     d.dirwts1,
                                                                     d.vcd,
                                                                     d.hcd,
                                                                     d.dgintv,
                                                                     d.dginth,
                                                                     d.hvwt,
                                                                     rr1,
                                                                     cc1));
            RUN_TIMED_STAGE(nyquistTestMs,
                            k_nyquist_test<<<grid, block>>>(d.cddiffsq,
                                                            d.delhvsqsum,
                                                            d.nyquist,
                                                            rr1,
                                                            cc1));
            RUN_TIMED_STAGE(nyquistRefineMs,
                            k_nyquist_refine_row_prefix<<<1, kNyquistPrefixThreads>>>(
                                d.nyquist,
                                rr1,
                                cc1));
            RUN_TIMED_STAGE(nyquistAreaMs,
                            k_nyquist_area_interpolation<<<grid, block>>>(d.cfa,
                                                                          d.nyquist,
                                                                          d.hvwt,
                                                                          rr1,
                                                                          cc1));
            RUN_TIMED_HOST_STAGE(greenPlaneMs,
                            launch_green_plane_assembly(
                                grid,
                                block,
                                d.cfa,
                                d.vcd,
                                d.hcd,
                                d.nyquist,
                                d.hvwt,
                                d.dgrb0,
                                d.rgbgreen,
                                d.dgrb2h,
                                d.dgrb2v,
                                rr1,
                                cc1));
            RUN_TIMED_STAGE(nyquistGreenMs,
                            k_nyquist_green_refinement<<<grid, block>>>(d.cfa,
                                                                        d.vcd,
                                                                        d.hcd,
                                                                        d.nyquist,
                                                                        d.dgrb2h,
                                                                        d.dgrb2v,
                                                                        d.dgrb0,
                                                                        d.rgbgreen,
                                                                        rr1,
                                                                        cc1));
            RUN_TIMED_STAGE(diagonalRbMs,
                            k_diagonal_rb_interpolation<<<grid, block>>>(d.cfa,
                                                                         d.delp,
                                                                         d.delm,
                                                                         d.dgrbsq1p,
                                                                         d.dgrbsq1m,
                                                                         d.rbm,
                                                                         d.rbp,
                                                                         d.pmwt,
                                                                         rr1,
                                                                         cc1));
            RUN_TIMED_HOST_STAGE(pmwtRbintMs,
                            launch_pmwt_refinement_and_rbint(
                                grid,
                                block,
                                d.cfa,
                                d.rbm,
                                d.rbp,
                                d.pmwt,
                                d.pmwtalt,
                                d.rbint,
                                rr1,
                                cc1));
            RUN_TIMED_STAGE(diagonalGreenMs,
                            k_diagonal_green_correction<<<grid, block>>>(d.cfa,
                                                                         d.dirwts0,
                                                                         d.dirwts1,
                                                                         d.hvwt,
                                                                         d.pmwt,
                                                                         d.rbint,
                                                                         d.dgrb0,
                                                                         d.rgbgreen,
                                                                         rr1,
                                                                         cc1));
            RUN_TIMED_STAGE(chromaCosetMs,
                            k_chrominance_coset_split<<<grid, block>>>(d.dgrb0,
                                                                       d.dgrb1,
                                                                       rr1,
                                                                       cc1));
            RUN_TIMED_STAGE(fancyChromaMs,
                            k_fancy_chrominance_interpolation<<<grid, block>>>(d.dgrb0,
                                                                              d.dgrb1,
                                                                              rr1,
                                                                              cc1));
            RUN_TIMED_STAGE(finalOutputMs,
                            k_final_output_planes<<<grid, block>>>(d.rgbgreen,
                                                                   d.hvwt,
                                                                   d.dgrb0,
                                                                   d.dgrb1,
                                                                   d.red,
                                                                   d.green,
                                                                   d.blue,
                                                                   rr1,
                                                                   cc1));
            ++totals->tiles;
        }
    }

#undef RUN_TIMED_HOST_STAGE
#undef RUN_TIMED_STAGE

    CK(cudaEventDestroy(stop));
    CK(cudaEventDestroy(start));
}

void print_timing_stage(const char * name,
                        double ms,
                        const TimedStageTotals & totals,
                        double totalMs,
                        double * dominantMs,
                        const char ** dominantName)
{
    const double frameMs =
        totals.iterations > 0 ? ms / static_cast<double>(totals.iterations) : 0.0;
    const double tileMs =
        totals.tiles > 0 ? ms / static_cast<double>(totals.tiles) : 0.0;
    const double percent = totalMs > 0.0 ? (100.0 * ms / totalMs) : 0.0;
    const double fpsEquivalent = frameMs > 0.0 ? (1000.0 / frameMs) : 0.0;
    std::cout << "[amaze-stage-timing] stage=" << name
              << " total_ms=" << std::fixed << std::setprecision(3) << ms
              << " avg_frame_ms=" << frameMs
              << " avg_tile_ms=" << tileMs
              << " pct=" << percent
              << " fps_equiv=" << fpsEquivalent << "\n";
    if (ms > *dominantMs)
    {
        *dominantMs = ms;
        *dominantName = name;
    }
}

bool run_timing_profile(int iterations)
{
    if (iterations < 1) iterations = 1;
    const CaseSpec spec = {"synthetic_full_frame", 1808, 2268, -16, -16};
    const std::vector<uint16_t> raw = make_raw_pattern(spec.width,
                                                       spec.height,
                                                       0xC0DA5120u);

    DeviceBuffers d;
    allocate_device(&d, raw.size());
    CK(cudaMemcpy(d.raw,
                  raw.data(),
                  raw.size() * sizeof(uint16_t),
                  cudaMemcpyHostToDevice));

    TimedStageTotals warmup;
    run_timed_frame_once(spec, d.raw, d, &warmup);

    TimedStageTotals totals;
    totals.iterations = iterations;
    for (int i = 0; i < iterations; ++i)
    {
        run_timed_frame_once(spec, d.raw, d, &totals);
    }
    CK(cudaDeviceSynchronize());
    free_device(&d);

    const double totalMs = timed_total_ms(totals);
    const double avgFrameMs =
        iterations > 0 ? totalMs / static_cast<double>(iterations) : 0.0;
    const double fpsEquivalent = avgFrameMs > 0.0 ? (1000.0 / avgFrameMs) : 0.0;
    double dominantMs = -1.0;
    const char * dominantName = "none";

    std::cout << "\n[amaze-stage-timing] schema=mlvapp.cuda_amaze_stage_timing.v1"
              << " image=" << spec.width << "x" << spec.height
              << " iterations=" << iterations
              << " tiles_per_iteration=" << (totals.tiles / iterations)
              << " total_tiles=" << totals.tiles
              << " avg_frame_stage_ms=" << std::fixed << std::setprecision(3)
              << avgFrameMs
              << " fps_equiv=" << fpsEquivalent << "\n";
    print_timing_stage("clear", totals.clearMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("tile_load", totals.loadMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("gradients", totals.gradientsMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("diagonal_precursors", totals.diagonalPrecursorsMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("green_interpolation", totals.greenInterpolationMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("variance_selection", totals.varianceSelectionMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("hvwt_adaptive", totals.hvwtAdaptiveMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("nyquist_test", totals.nyquistTestMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("nyquist_refine", totals.nyquistRefineMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("nyquist_area", totals.nyquistAreaMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("green_plane", totals.greenPlaneMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("nyquist_green", totals.nyquistGreenMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("diagonal_rb", totals.diagonalRbMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("pmwt_rbint", totals.pmwtRbintMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("diagonal_green", totals.diagonalGreenMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("chroma_coset", totals.chromaCosetMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("fancy_chroma", totals.fancyChromaMs, totals, totalMs, &dominantMs, &dominantName);
    print_timing_stage("final_output", totals.finalOutputMs, totals, totalMs, &dominantMs, &dominantName);
    std::cout << "[amaze-stage-timing] dominant_stage=" << dominantName
              << " dominant_total_ms=" << dominantMs
              << " dominant_avg_frame_ms="
              << (dominantMs / static_cast<double>(iterations))
              << "\n";
    return true;
}
}

#ifndef CUDA_AMAZE_DEBAYER_STAGE_PROBE_NO_MAIN
int main(int argc, char ** argv)
{
    bool timing = false;
    int timingIterations = 3;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i] ? argv[i] : "";
        const std::string prefix = "--timing-iterations=";
        if (arg == "--timing")
        {
            timing = true;
        }
        else if (arg.find(prefix) == 0)
        {
            timingIterations = std::atoi(arg.c_str() + prefix.size());
            if (timingIterations < 1) timingIterations = 1;
        }
        else
        {
            std::fprintf(stderr,
                         "usage: cuda_amaze_debayer_stage_probe.exe [--timing] [--timing-iterations=N]\n");
            return 2;
        }
    }

    int deviceCount = 0;
    CK(cudaGetDeviceCount(&deviceCount));
    if (deviceCount < 1)
    {
        std::fprintf(stderr, "No CUDA devices found.\n");
        return 2;
    }

    CK(cudaSetDevice(0));
    cudaDeviceProp prop;
    CK(cudaGetDeviceProperties(&prop, 0));
    std::cout << "=== cuda_amaze_debayer_stage_probe ===\n"
              << "device=" << prop.name << " sm_" << prop.major << prop.minor
              << " tolerance=" << kTolerance << "\n";

    const CaseSpec cases[] = {
        {"top_left_halo", 1808, 2268, -16, -16},
        {"interior_full_tile", 1808, 2268, 112, 112},
        {"bottom_right_halo", 1808, 2268, 2160, 1680},
        {"odd_right_halo", 1809, 2268, 2160, 1680},
    };

    bool ok = true;
    for (const CaseSpec & spec : cases)
    {
        ok = run_case(spec) && ok;
    }

    if (ok && timing)
    {
        ok = run_timing_profile(timingIterations) && ok;
    }

    std::cout << "\n[amaze-stage-probe] RESULT: "
              << (ok ? "PASS" : "FAIL")
              << " (generic AMaZE tile/gradient/green-interpolation/variance-selection/hvwt/nyquist/area/green/nyquist-green/diagonal-rb/pmwtalt-rbint/diagonal-green-correction/chrominance/final-output stages)\n";
    return ok ? 0 : 1;
}
#endif
