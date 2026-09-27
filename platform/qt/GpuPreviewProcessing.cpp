#include "GpuPreviewProcessing.h"

#include "../../src/processing/raw_processing.h"

#include <QGuiApplication>
#include <QOffscreenSurface>
#include <QOpenGLContext>
#include <QOpenGLFramebufferObject>
#include <QOpenGLFramebufferObjectFormat>
#include <QOpenGLFunctions>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QSurfaceFormat>
#include <QVector2D>
#include <QVector3D>
#include <QtGlobal>

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <limits>
#include <vector>
#include <omp.h>

namespace
{
constexpr float kRec709RgbToY[3] = {
    0.2126729f,
    0.7151522f,
    0.0721750f
};
constexpr int kLutTextureEdge = 256;
constexpr GLfloat kQuadVertices[16] = {
    -1.0f,  1.0f, 0.0f, 1.0f,
     1.0f,  1.0f, 1.0f, 1.0f,
    -1.0f, -1.0f, 0.0f, 0.0f,
     1.0f, -1.0f, 1.0f, 0.0f,
};

bool envFlagEnabled(const QByteArray & value)
{
    if ( value.isEmpty() ) return false;

    const QByteArray normalized = value.trimmed().toLower();
    return normalized == "1"
        || normalized == "true"
        || normalized == "yes"
        || normalized == "on";
}

uint64_t fnv1a64_append(uint64_t hash, const void * data, size_t size)
{
    const uint8_t * bytes = static_cast<const uint8_t *>(data);
    for (size_t index = 0; index < size; ++index)
    {
        hash ^= bytes[index];
        hash *= 1099511628211ull;
    }
    return hash;
}

float clamp01(float value)
{
    return std::max(0.0f, std::min(1.0f, value));
}

float clamp16(float value)
{
    return std::max(0.0f, std::min(65535.0f, value));
}

float reinhardTonemap(float value)
{
    return (value < 0.0f) ? value : value / (1.0f + value);
}

float reinhardForColour(float value)
{
    return (value < 0.5f)
        ? value
        : (reinhardTonemap((value - 0.5f) / 0.5f) * 0.5f + 0.5f);
}

float reinhardForBlue(float value)
{
    return (value < 0.7f)
        ? value
        : (reinhardTonemap((value - 0.7f) / 0.3f) * 0.3f + 0.7f);
}

const uint16_t * lutValues(const QByteArray & bytes)
{
    return reinterpret_cast<const uint16_t *>(bytes.constData());
}

uint16_t sampleLut(const QByteArray & lutBytes, float normalized)
{
    if ( lutBytes.size() < static_cast<int>(65536u * sizeof(uint16_t)) )
    {
        return 0;
    }

    const int index = std::max(0, std::min(65535,
        static_cast<int>(normalized * 65535.0f + 0.5f)));
    return lutValues(lutBytes)[index];
}

float sampleNormalizedLut(const QByteArray & lutBytes, float normalized)
{
    if ( lutBytes.size() < static_cast<int>(65536u * sizeof(uint16_t)) )
    {
        return 0.0f;
    }

    const int index = std::max(0, std::min(65535,
        static_cast<int>(normalized * 65535.0f + 0.5f)));
    return reinterpret_cast<const uint16_t *>(lutBytes.constData())[index] / 65535.0f;
}

/* Hue-vs / luma-vs creative curves are float[36000] in [-1,1]. The CPU
 * reference reads them directly; the GPU uploads each as an R32F texture and
 * samples by the same integer index, so both stay bit-aligned. The luma index
 * is clamped to [0,35999]: hue_vs_luma can boost V (hsv[2]) to >= 1.0, and the
 * production path then indexes luma_vs_saturation[] out of bounds (the array is
 * exactly 36000 entries, and V == 1.0 already yields index 36000). That read is
 * undefined on the CPU, so both this reference and the shader clamp to the last
 * valid sample instead of matching undefined behaviour. */
constexpr int kHueVsCurveSamples = 36000;

float sampleHueVsCurve(const QByteArray & curveBytes, int index)
{
    if ( curveBytes.size() < static_cast<int>(kHueVsCurveSamples * sizeof(float)) )
    {
        return 0.0f;
    }
    const int clamped = std::max(0, std::min(kHueVsCurveSamples - 1, index));
    return reinterpret_cast<const float *>(curveBytes.constData())[clamped];
}

/* In-loop simple-contrast factor: processing->contrast_curve is a double[65536]
 * of per-luma exposure multipliers (raw_processing.c:2954), indexed by the
 * integer luma of the matrix-applied pixel. Carried as float[65536]; the factor
 * is a smooth exposure multiply and the result is quantised to uint16, so the
 * double->float narrowing is well within tolerance. */
constexpr int kInLoopContrastSamples = 65536;

float sampleInLoopContrastFactor(const QByteArray & curveBytes, int index)
{
    if ( curveBytes.size() < static_cast<int>(kInLoopContrastSamples * sizeof(float)) )
    {
        return 1.0f;
    }
    const int clamped = std::max(0, std::min(kInLoopContrastSamples - 1, index));
    return reinterpret_cast<const float *>(curveBytes.constData())[clamped];
}

float sampleShadowsHighlightsFactor(const GpuPreviewProcessingConfig & config,
                                    int pixelIndex)
{
    if ( !config.applyShadowsHighlights
      || !config.shadowsHighlightsFrameStateReady
      || config.shadowsHighlightsBlur.size()
            < static_cast<int>((static_cast<size_t>(pixelIndex) + 1u) * 3u * sizeof(uint16_t)) )
    {
        return 1.0f;
    }

    const uint16_t * blur =
        reinterpret_cast<const uint16_t *>(config.shadowsHighlightsBlur.constData())
        + static_cast<size_t>(pixelIndex) * 3u;
    int bval = 0;
    if ( config.shadowsHighlightsCurveIndexMask )
    {
        bval = blur[0];
    }
    else
    {
        const int32_t blurR =
            static_cast<int32_t>(lutValues(config.matrixLutR)[blur[0]]);
        const int32_t blurG =
            static_cast<int32_t>(lutValues(config.matrixLutG)[blur[1]]);
        const int32_t blurB =
            static_cast<int32_t>(lutValues(config.matrixLutB)[blur[2]]);
        bval = ((blurR << 2) + (blurG * 11) + blurB) >> 4;
    }

    return sampleInLoopContrastFactor(config.shadowsHighlightsCurve, bval);
}

/* Float32 mirror of fromRGBtoHSV (processing.c:282-308): hsv[0]=H in [0,360),
 * hsv[1]=S, hsv[2]=V. Kept byte-identical (same >= tie ordering) so the CPU
 * reference and the GLSL port agree. */
void previewFromRGBtoHSV(const float rgb[3], float hsv[3])
{
    hsv[0] = 0.0f;
    hsv[2] = std::max(rgb[0], std::max(rgb[1], rgb[2]));
    const float delta = hsv[2] - std::min(rgb[0], std::min(rgb[1], rgb[2]));
    if ( delta < FLT_MIN )
    {
        hsv[1] = 0.0f;
    }
    else
    {
        hsv[1] = delta / hsv[2];
        if ( rgb[0] >= hsv[2] )
        {
            hsv[0] = (rgb[1] - rgb[2]) / delta;
            if ( hsv[0] < 0.0f ) hsv[0] += 6.0f;
        }
        else if ( rgb[1] >= hsv[2] )
        {
            hsv[0] = 2.0f + (rgb[2] - rgb[0]) / delta;
        }
        else
        {
            hsv[0] = 4.0f + (rgb[0] - rgb[1]) / delta;
        }
    }
    hsv[0] *= 60.0f;
}

/* Float32 mirror of fromHSVtoRGB (processing.c:310-363). */
void previewFromHSVtoRGB(const float hsv[3], float rgb[3])
{
    if ( hsv[1] < FLT_MIN )
    {
        rgb[0] = rgb[1] = rgb[2] = hsv[2];
        return;
    }
    const float h = hsv[0] / 60.0f;
    const int i = static_cast<int>(h);
    const float f = h - static_cast<float>(i);
    const float p = hsv[2] * (1.0f - hsv[1]);
    if ( i & 1 )
    {
        const float q = hsv[2] * (1.0f - (hsv[1] * f));
        if ( i == 1 ) { rgb[0] = q; rgb[1] = hsv[2]; rgb[2] = p; }
        else if ( i == 3 ) { rgb[0] = p; rgb[1] = q; rgb[2] = hsv[2]; }
        else { rgb[0] = hsv[2]; rgb[1] = p; rgb[2] = q; }
    }
    else
    {
        const float t = hsv[2] * (1.0f - (hsv[1] * (1.0f - f)));
        if ( i == 0 ) { rgb[0] = hsv[2]; rgb[1] = t; rgb[2] = p; }
        else if ( i == 2 ) { rgb[0] = p; rgb[1] = hsv[2]; rgb[2] = t; }
        else { rgb[0] = t; rgb[1] = p; rgb[2] = hsv[2]; }
    }
}

/* Linear interpolation matching cube_lut.c lerp() for the 1D LUT path. */
float lutLerp(float x, float x1, float x2, float q00, float q01)
{
    if ( (x2 - x1) == 0.0f ) return q00;
    return ((x2 - x) / (x2 - x1)) * q00 + ((x - x1) / (x2 - x1)) * q01;
}

/* Reinhard gamut compression on a white-balanced float triplet
 * (raw_processing.c:3173-3200), shared by the base and gradient layers and
 * mirrored by the in-shader gamut block. */
void applyPreviewGamutCompression(float wb[3], const float rgbToY[3])
{
    const float y = rgbToY[0] * wb[0] + rgbToY[1] * wb[1] + rgbToY[2] * wb[2];
    const float minChannel = std::min(std::min(wb[0], wb[1]), wb[2]);
    float gamutReference[3];
    for (int channel = 0; channel < 3; ++channel)
    {
        const float yToMinChannel = (y != 0.0f) ? ((y - wb[channel]) / y) : 0.0f;
        const float tonemapped = (channel == 0)
            ? reinhardForColour(yToMinChannel)
            : reinhardForBlue(yToMinChannel);
        gamutReference[channel] = -(tonemapped * y) + y;
    }
    const float gamutMin =
        std::min(std::min(gamutReference[0], gamutReference[1]), gamutReference[2]);
    float desaturateFactor = 1.0f;
    const float denominator = y - minChannel;
    if ( y > 0.0f && std::fabs(denominator) > 1e-8f )
    {
        desaturateFactor = (y - gamutMin) / denominator;
    }
    for (int channel = 0; channel < 3; ++channel)
    {
        wb[channel] = (wb[channel] - y) * desaturateFactor + y;
    }
}

/* CPU box blur replicating the engine blur_image bit-exactly: separable, the
 * window for output j is [j-r+1, j+r+1] clamped (the engine's off-by-one shift),
 * truncating integer divide, uint16 intermediate between the two passes.
 * Disabled channels pass through. Mirrors gpuPreviewProcessingApplyBoxBlurOffscreen
 * (validated 0-LSB vs blur_image). */
void cpuBoxBlur(uint16_t * img, int width, int height, int radius, bool doR, bool doG, bool doB)
{
    if ( radius <= 0 ) return;
    const int diameter = 2 * radius + 1;
    const bool ch[3] = { doR, doG, doB };
    std::vector<uint16_t> temp(static_cast<size_t>(width) * height * 3u);
    for (int y = 0; y < height; ++y)
        for (int x = 0; x < width; ++x)
            for (int c = 0; c < 3; ++c)
            {
                if ( ch[c] )
                {
                    int sum = 0;
                    for (int k = 0; k < diameter; ++k)
                    {
                        int xx = x - radius + 1 + k;
                        xx = xx < 0 ? 0 : (xx >= width ? width - 1 : xx);
                        sum += img[(y * width + xx) * 3 + c];
                    }
                    temp[(y * width + x) * 3 + c] = static_cast<uint16_t>(sum / diameter);
                }
                else
                {
                    temp[(y * width + x) * 3 + c] = img[(y * width + x) * 3 + c];
                }
            }
    for (int y = 0; y < height; ++y)
        for (int x = 0; x < width; ++x)
            for (int c = 0; c < 3; ++c)
            {
                if ( ch[c] )
                {
                    int sum = 0;
                    for (int k = 0; k < diameter; ++k)
                    {
                        int yy = y - radius + 1 + k;
                        yy = yy < 0 ? 0 : (yy >= height ? height - 1 : yy);
                        sum += temp[(yy * width + x) * 3 + c];
                    }
                    img[(y * width + x) * 3 + c] = static_cast<uint16_t>(sum / diameter);
                }
                else
                {
                    img[(y * width + x) * 3 + c] = temp[(y * width + x) * 3 + c];
                }
            }
}

inline int chromaClamp16(int v)
{
    return v < 0 ? 0 : (v > 65535 ? 65535 : v);
}

/* CPU chroma separation/blur post-pass replicating raw_processing.c:1832-1971:
 * RGB->YCbCr (JPEG transform via truncated-int terms), optional box blur of
 * Cb/Cr, YCbCr->RGB. Uses double*(coeff) truncated to int exactly like the engine
 * cs_zone LUTs ((int32)((double)j*coeff)). The GPU path uses RGBA32F LUTs holding
 * the identical truncated integers, so all three (CPU/GPU/engine) agree. */
void cpuChromaPostPass(uint16_t * img, int width, int height, int radius)
{
    const int n = width * height;
    for (int i = 0; i < n; ++i)
    {
        const int R = img[i * 3 + 0];
        const int G = img[i * 3 + 1];
        const int B = img[i * 3 + 2];
        const int Y  = static_cast<int>(static_cast<double>(R) * 0.299)
                     + static_cast<int>(static_cast<double>(G) * 0.587)
                     + static_cast<int>(static_cast<double>(B) * 0.114);
        const int Cb = 32768 + static_cast<int>(static_cast<double>(R) * -0.168736)
                     + static_cast<int>(static_cast<double>(G) * -0.331264) + (B >> 1);
        const int Cr = 32768 + (R >> 1) + static_cast<int>(static_cast<double>(G) * -0.418688)
                     + static_cast<int>(static_cast<double>(B) * -0.081312);
        img[i * 3 + 0] = static_cast<uint16_t>(chromaClamp16(Y));
        img[i * 3 + 1] = static_cast<uint16_t>(chromaClamp16(Cb));
        img[i * 3 + 2] = static_cast<uint16_t>(chromaClamp16(Cr));
    }
    if ( radius > 0 )
    {
        cpuBoxBlur(img, width, height, radius, false, true, true);
    }
    for (int i = 0; i < n; ++i)
    {
        const int Y  = img[i * 3 + 0];
        const int Cb = img[i * 3 + 1];
        const int Cr = img[i * 3 + 2];
        const int R = Y + static_cast<int>(static_cast<double>(Cr - 32768) * 1.402);
        const int G = Y + static_cast<int>(static_cast<double>(Cb - 32768) * -0.344136)
                        + static_cast<int>(static_cast<double>(Cr - 32768) * -0.714136);
        const int B = Y + static_cast<int>(static_cast<double>(Cb - 32768) * 1.772);
        img[i * 3 + 0] = static_cast<uint16_t>(chromaClamp16(R));
        img[i * 3 + 1] = static_cast<uint16_t>(chromaClamp16(G));
        img[i * 3 + 2] = static_cast<uint16_t>(chromaClamp16(B));
    }
}

/* CPU sharpen post-pass replicating raw_processing.c:1849-1965 (standalone: no
 * chroma separation, no sobel mask): a fixed 5-tap cross,
 * sharp = ka[center] - ky[up] - ky[down] - kx[left] - kx[right], clamped.
 * ka=(uint32)(v*a), kx/ky=LIMIT16(v*x/y). First/last column pass through; rows
 * clamp to edge. The engine's bottom row is UB (it sets p_row instead of n_row,
 * leaving n_row uninitialized); the GPU/CPU use a clean clamp instead (documented
 * divergence on the bottom row only, like the vignette per-chunk note). */
void cpuSharpenPostPass(uint16_t * img, int width, int height, double a, double x, double y)
{
    std::vector<uint16_t> src(img, img + static_cast<size_t>(width) * height * 3u);
    auto ka = [&](int v) -> int { return static_cast<int>(static_cast<uint32_t>(static_cast<double>(v) * a)); };
    auto kx = [&](int v) -> int { int t = static_cast<int>(static_cast<double>(v) * x); return t < 0 ? 0 : (t > 65535 ? 65535 : t); };
    auto ky = [&](int v) -> int { int t = static_cast<int>(static_cast<double>(v) * y); return t < 0 ? 0 : (t > 65535 ? 65535 : t); };
    for (int yy = 0; yy < height; ++yy)
    {
        const int up = (yy == 0) ? 0 : yy - 1;
        const int dn = (yy == height - 1) ? height - 1 : yy + 1;
        for (int xx = 0; xx < width; ++xx)
            for (int c = 0; c < 3; ++c)
            {
                if ( xx == 0 || xx == width - 1 )
                {
                    img[(yy * width + xx) * 3 + c] = src[(yy * width + xx) * 3 + c];
                    continue;
                }
                const int center = src[(yy * width + xx) * 3 + c];
                const int left  = src[(yy * width + (xx - 1)) * 3 + c];
                const int right = src[(yy * width + (xx + 1)) * 3 + c];
                const int u = src[(up * width + xx) * 3 + c];
                const int d = src[(dn * width + xx) * 3 + c];
                const int sharp = ka(center) - ky(u) - ky(d) - kx(left) - kx(right);
                img[(yy * width + xx) * 3 + c] = static_cast<uint16_t>(chromaClamp16(sharp));
            }
    }
}

/* CPU median denoise post-pass replicating denoise_2D_median_with_context
 * (denoiser_2d_median.c:132-225): per-pixel square-window median (the middle order
 * statistic) blended with the original by strength/100; border pixels (within
 * window/2 of an edge) untouched. */
void cpuMedianPostPass(uint16_t * img, int width, int height, int window, int strength)
{
    if ( strength > 100 ) strength = 100;
    if ( strength == 0 || window == 0 ) return;
    const float strengthF = strength / 100.0f;
    const float antiStrengthF = 1.0f - strengthF;
    const int winSize = window * window;
    const int edge = window / 2;
    const int middle = winSize / 2;
    if ( width <= edge * 2 || height <= edge * 2 ) return;

    std::vector<uint16_t> noisy(img, img + static_cast<size_t>(width) * height * 3u);
    std::vector<int> winR(winSize), winG(winSize), winB(winSize);
    for (int x = edge; x < width - edge; ++x)
        for (int y = edge; y < height - edge; ++y)
        {
            int idx = 0;
            for (int fx = 0; fx < window; ++fx)
                for (int fy = 0; fy < window; ++fy)
                {
                    const int ww = x + fx - edge;
                    const int hh = y + fy - edge;
                    winR[idx] = noisy[(hh * width + ww) * 3 + 0];
                    winG[idx] = noisy[(hh * width + ww) * 3 + 1];
                    winB[idx] = noisy[(hh * width + ww) * 3 + 2];
                    ++idx;
                }
            std::nth_element(winR.begin(), winR.begin() + middle, winR.end());
            std::nth_element(winG.begin(), winG.begin() + middle, winG.end());
            std::nth_element(winB.begin(), winB.begin() + middle, winB.end());
            img[(y * width + x) * 3 + 0] = static_cast<uint16_t>(strengthF * winR[middle] + antiStrengthF * noisy[(y * width + x) * 3 + 0]);
            img[(y * width + x) * 3 + 1] = static_cast<uint16_t>(strengthF * winG[middle] + antiStrengthF * noisy[(y * width + x) * 3 + 1]);
            img[(y * width + x) * 3 + 2] = static_cast<uint16_t>(strengthF * winB[middle] + antiStrengthF * noisy[(y * width + x) * 3 + 2]);
        }
}

void applyPreviewProcessingPixel(const GpuPreviewProcessingConfig & config,
                                 const uint16_t * inputPixel,
                                 uint16_t * outputPixel,
                                 int pixelIndex)
{
    float color[3];
    for (int channel = 0; channel < 3; ++channel)
    {
        color[channel] =
            sampleLut(config.levelsLut, inputPixel[channel] / 65535.0f) / 65535.0f;
    }

    float matrixApplied[3];
    matrixApplied[0] = sampleNormalizedLut(config.matrixLutR, color[0]);
    matrixApplied[1] = sampleNormalizedLut(config.matrixLutG, color[1]);
    matrixApplied[2] = sampleNormalizedLut(config.matrixLutB, color[2]);

    /* tmp1 for highlight reconstruction = the diagonal-matrix green BEFORE the
     * vignette/contrast expo multiplies (raw_processing.c:3008 tmp1 = wb_g). */
    const float reconMatrixGreen = matrixApplied[1];

    /* The gradient layer reuses the SHARED expo_correction (vignette x base
     * in-loop-contrast) and the base luma index cval. Capture them here. */
    float sharedVignetteFactor = 1.0f;
    float sharedShadowsHighlightsFactor = 1.0f;
    float sharedContrastFactor = 1.0f;
    int sharedCval = 0;
    bool sharedHaveCval = false;

    if ( config.applyVignette )
    {
        /* Vignette: a per-pixel exposure multiply, the first expo_correction
         * contributor (raw_processing.c:2881-2889). The engine pre-increments the
         * mask pointer before reading, so pixel i uses mask[i+1]; the last pixel
         * (i+1 >= count) skips the multiply (the vignette_end guard). expo_correction
         * is applied to the matrix value before camera-WB, so it multiplies
         * matrixApplied here (commutes with the in-loop-contrast multiply). The
         * whole-frame flat index matches the GPU's single-pass contract (the engine's
         * per-OpenMP-chunk off-by-one is not reproduced; documented). */
        const int maskCount = static_cast<int>(config.vignetteMask.size() / static_cast<int>(sizeof(float)));
        const int maskIdx = pixelIndex + 1;
        if ( maskIdx < maskCount )
        {
            const float m = reinterpret_cast<const float *>(config.vignetteMask.constData())[maskIdx];
            const double base = 1.0 + (static_cast<double>(m) * config.vignetteStrength / 128.0);
            const float vfactor = static_cast<float>(std::pow(base, 4.0));
            sharedVignetteFactor = vfactor;
            matrixApplied[0] *= vfactor;
            matrixApplied[1] *= vfactor;
            matrixApplied[2] *= vfactor;
        }
    }

    if ( config.applyShadowsHighlights )
    {
        sharedShadowsHighlightsFactor =
            sampleShadowsHighlightsFactor(config, pixelIndex);
    }

    if ( config.applyInLoopContrast || config.applyGradientContrast )
    {
        /* In-loop simple-contrast factor (raw_processing.c:2941-2954): a per-pixel
         * exposure multiply by contrast_curve[cval], where cval is the integer
         * luma (4R+11G+B)>>4 of the matrix-applied (pre camera-WB) pixel. Applied
         * to the matrix value before the camera matrix and gamma, matching
         * pix0 = wb_r * expo_correction. The cval is also shared by the gradient
         * layer (base contrast feeds the shared expo_correction; gradient contrast
         * uses the same index into its own curve), so it is computed whenever
         * either the base or the gradient contrast is active. */
        const int32_t matR = static_cast<int32_t>(matrixApplied[0] * 65535.0f + 0.5f);
        const int32_t matG = static_cast<int32_t>(matrixApplied[1] * 65535.0f + 0.5f);
        const int32_t matB = static_cast<int32_t>(matrixApplied[2] * 65535.0f + 0.5f);
        sharedCval = ((matR << 2) + (matG * 11) + matB) >> 4;
        sharedHaveCval = true;
        if ( config.applyInLoopContrast )
        {
            const float factor = sampleInLoopContrastFactor(config.inLoopContrastCurve, sharedCval);
            sharedContrastFactor = factor;
            matrixApplied[0] *= factor;
            matrixApplied[1] *= factor;
            matrixApplied[2] *= factor;
        }
    }

    if ( config.applyShadowsHighlights )
    {
        matrixApplied[0] *= sharedShadowsHighlightsFactor;
        matrixApplied[1] *= sharedShadowsHighlightsFactor;
        matrixApplied[2] *= sharedShadowsHighlightsFactor;
    }

    if ( config.applyHighlightReconstruction )
    {
        /* Highlight reconstruction (raw_processing.c:3088-3119): for a clipped
         * green it replaces green with (R+B)/2. The engine applies it to the
         * uint16-quantized diagonal*expo pixel (pix[i] = (uint16)LIMIT16(...)) and
         * keys on tmp1 = (uint16)LIMIT16(diagonal green pre-expo), comparing
         * against the static white-level green (highest_green) or, for dual-ISO,
         * a +/-5000 window around the per-frame highest_green_diso peak plus the
         * pix[1]<1.1*pix[0] && pix[1]<pix[2] green-dominance guard. Enabling recon
         * forces the engine's general loop (uint16-quantize before the 3x3); the
         * subset models the float-3x3 fast path, a structural delta already inside
         * the parity tolerance for the vignette/AgX slices. LIMIT16 is clamp-only;
         * the (uint16) cast truncates toward zero (floor on the clamped value). */
        const float p0 = std::floor(clamp16(matrixApplied[0] * 65535.0f));
        const float p1 = std::floor(clamp16(matrixApplied[1] * 65535.0f));
        const float p2 = std::floor(clamp16(matrixApplied[2] * 65535.0f));
        const float tmp1 = std::floor(clamp16(reconMatrixGreen * 65535.0f + 0.5f));
        bool replace = false;
        if ( config.highlightReconDualIso )
        {
            const float lo = clamp16(static_cast<float>(config.highestGreenDiso) - 5000.0f);
            const float hi = clamp16(static_cast<float>(config.highestGreenDiso) + 5000.0f);
            if ( tmp1 >= lo && tmp1 <= hi && p1 < 1.1f * p0 && p1 < p2 )
            {
                replace = true;
            }
        }
        else if ( tmp1 == static_cast<float>(config.highestGreen) )
        {
            replace = true;
        }
        if ( replace )
        {
            matrixApplied[1] = std::floor((p0 + p2) / 2.0f) / 65535.0f;
        }
    }

    if ( config.useCameraMatrix )
    {
        float wbApplied[3];
        wbApplied[0] = config.properWbMatrix[0] * matrixApplied[0]
                     + config.properWbMatrix[1] * matrixApplied[1]
                     + config.properWbMatrix[2] * matrixApplied[2];
        wbApplied[1] = config.properWbMatrix[3] * matrixApplied[0]
                     + config.properWbMatrix[4] * matrixApplied[1]
                     + config.properWbMatrix[5] * matrixApplied[2];
        wbApplied[2] = config.properWbMatrix[6] * matrixApplied[0]
                     + config.properWbMatrix[7] * matrixApplied[1]
                     + config.properWbMatrix[8] * matrixApplied[2];

        if ( config.applyGamutCompression )
        {
            applyPreviewGamutCompression(wbApplied, config.rgbToY);
        }

        std::memcpy(matrixApplied, wbApplied, sizeof(matrixApplied));
    }

    if ( config.applyAgx )
    {
        /* AgX forward, after WB/gamut and before gamma (raw_processing_8bit_kernel
         * .inc:264-281): clip negatives, scale to 16-bit, apply the compressed-
         * gamut matrix (double accumulation, float coeffs), then (uint16_t)LIMIT16
         * = clamp+truncate, and renormalize so the gamma LUT indexes identically.
         * preview routes through the direct8 kernel that this mirrors. */
        double v[3];
        for (int channel = 0; channel < 3; ++channel)
        {
            v[channel] = static_cast<double>(matrixApplied[channel]) * 65535.0;
            if ( v[channel] < 0.0 ) v[channel] = 0.0;
        }
        const double a[3] = {
            v[0] * config.agxForward[0] + v[1] * config.agxForward[1] + v[2] * config.agxForward[2],
            v[0] * config.agxForward[3] + v[1] * config.agxForward[4] + v[2] * config.agxForward[5],
            v[0] * config.agxForward[6] + v[1] * config.agxForward[7] + v[2] * config.agxForward[8] };
        for (int channel = 0; channel < 3; ++channel)
        {
            const double clamped = a[channel] < 0.0 ? 0.0 : (a[channel] > 65535.0 ? 65535.0 : a[channel]);
            matrixApplied[channel] = static_cast<float>(static_cast<uint16_t>(clamped)) / 65535.0f;
        }
    }

    uint16_t gammaOut[3];
    for (int channel = 0; channel < 3; ++channel)
    {
        const float gammaInput = clamp01(matrixApplied[channel]);
        gammaOut[channel] = sampleLut(config.gammaLut, gammaInput);
    }

    if ( config.applyGradient )
    {
        /* Gradient layer (raw_processing.c:3021-3078 + 3311-3492): a second copy of
         * the pre-creative pipeline through the gradient LUTs, blended into the base
         * in gamma space by the per-pixel mask BEFORE the creative chain (which runs
         * once on the blended result). Shares proper_wb / gamut weights / AgX
         * matrices and the shared expo_correction (vignette x base contrast); adds a
         * gradient-contrast factor, a gradient gamma LUT, and gradient highlight-
         * recon green keys. The engine keeps the gradient pixel a fractional float
         * through Part 1 (LIMIT16 = clamp, not truncate), its recon divides in float,
         * and it truncates only at the gradient gamma index -- all mirrored here. The
         * gradient 3x3/gamut run in float (engine uses double; the <=1 LSB delta
         * after the gamma LUT is within the parity tolerance). */
        float g[3];
        g[0] = sampleNormalizedLut(config.gradientMatrixLutR, color[0]) * 65535.0f;
        g[1] = sampleNormalizedLut(config.gradientMatrixLutG, color[1]) * 65535.0f;
        g[2] = sampleNormalizedLut(config.gradientMatrixLutB, color[2]) * 65535.0f;
        const float gradTmpGreen = g[1];
        float gradContrastFactor = 1.0f;
        if ( config.applyGradientContrast && sharedHaveCval )
        {
            gradContrastFactor = sampleInLoopContrastFactor(config.gradientContrastCurve, sharedCval);
        }
        const float gradExpo = sharedVignetteFactor * sharedShadowsHighlightsFactor
                              * sharedContrastFactor * gradContrastFactor;
        g[0] = clamp16(g[0] * gradExpo);
        g[1] = clamp16(g[1] * gradExpo);
        g[2] = clamp16(g[2] * gradExpo);
        if ( config.applyHighlightReconstruction )
        {
            const float gt1 = std::floor(clamp16(gradTmpGreen + 0.5f));
            bool grep = false;
            if ( config.highlightReconDualIso )
            {
                const float lo = clamp16(static_cast<float>(config.gradientHighestGreenDiso) - 5000.0f);
                const float hi = clamp16(static_cast<float>(config.gradientHighestGreenDiso) + 5000.0f);
                if ( gt1 >= lo && gt1 <= hi && g[1] < 1.1f * g[0] && g[1] < g[2] ) grep = true;
            }
            else if ( gt1 == static_cast<float>(config.gradientHighestGreen) )
            {
                grep = true;
            }
            if ( grep ) g[1] = (g[0] + g[2]) / 2.0f;
        }
        float gm[3] = { g[0] / 65535.0f, g[1] / 65535.0f, g[2] / 65535.0f };
        if ( config.useCameraMatrix )
        {
            float w[3];
            w[0] = config.properWbMatrix[0] * gm[0] + config.properWbMatrix[1] * gm[1] + config.properWbMatrix[2] * gm[2];
            w[1] = config.properWbMatrix[3] * gm[0] + config.properWbMatrix[4] * gm[1] + config.properWbMatrix[5] * gm[2];
            w[2] = config.properWbMatrix[6] * gm[0] + config.properWbMatrix[7] * gm[1] + config.properWbMatrix[8] * gm[2];
            if ( config.applyGamutCompression )
            {
                applyPreviewGamutCompression(w, config.rgbToY);
            }
            gm[0] = w[0]; gm[1] = w[1]; gm[2] = w[2];
        }
        if ( config.applyAgx )
        {
            double v[3];
            for (int channel = 0; channel < 3; ++channel)
            {
                v[channel] = static_cast<double>(gm[channel]) * 65535.0;
                if ( v[channel] < 0.0 ) v[channel] = 0.0;
            }
            const double a[3] = {
                v[0] * config.agxForward[0] + v[1] * config.agxForward[1] + v[2] * config.agxForward[2],
                v[0] * config.agxForward[3] + v[1] * config.agxForward[4] + v[2] * config.agxForward[5],
                v[0] * config.agxForward[6] + v[1] * config.agxForward[7] + v[2] * config.agxForward[8] };
            for (int channel = 0; channel < 3; ++channel)
            {
                const double clamped = a[channel] < 0.0 ? 0.0 : (a[channel] > 65535.0 ? 65535.0 : a[channel]);
                gm[channel] = static_cast<float>(static_cast<uint16_t>(clamped)) / 65535.0f;
            }
        }
        const uint16_t * gradGamma = reinterpret_cast<const uint16_t *>(config.gradientGammaLut.constData());
        uint16_t pixg[3];
        for (int channel = 0; channel < 3; ++channel)
        {
            const int idx = static_cast<int>(clamp16(gm[channel] * 65535.0f));
            pixg[channel] = gradGamma[idx];
        }
        const float blend = (config.gradientMaskData != nullptr)
            ? (static_cast<float>(config.gradientMaskData[pixelIndex]) / 65535.0f) : 0.0f;
        for (int channel = 0; channel < 3; ++channel)
        {
            const float blended = blend * static_cast<float>(pixg[channel])
                                + (1.0f - blend) * static_cast<float>(gammaOut[channel]);
            gammaOut[channel] = static_cast<uint16_t>(blended);
        }
    }

    if ( config.applyHueVs )
    {
        /* Hue-vs / luma-vs creative curves, the FIRST creative stage, matching
         * raw_processing.c:3523-3578: RGB->HSV, four signed-curve adjustments
         * indexed by hue (H*100) and luma (V*36000), HSV->RGB, then rounded back
         * to uint16. Float32 mirrors the shader (+/-1 LSB vs the CPU double
         * pipeline, within engine tolerance). The luma curve index is clamped
         * (see sampleHueVsCurve) rather than matching the production OOB read. */
        float rgb[3];
        for (int channel = 0; channel < 3; ++channel)
        {
            rgb[channel] = static_cast<float>(gammaOut[channel]) / 65535.0f;
        }
        float hsl[3];
        previewFromRGBtoHSV(rgb, hsl);

        float sat = 0.0f;
        if ( !(gammaOut[0] == 0 && gammaOut[1] == 0 && gammaOut[2] == 0) )
        {
            const uint16_t biggest = std::max(std::max(gammaOut[0], gammaOut[1]), gammaOut[2]);
            const uint16_t smallest = std::min(std::min(gammaOut[0], gammaOut[1]), gammaOut[2]);
            sat = (static_cast<float>(biggest) - static_cast<float>(smallest))
                / static_cast<float>(biggest);
        }
        sat = 2.0f * sat / (sat * sat + 1.0f);
        if ( sat > 1.0f ) sat = 1.0f;

        const int hueIndex = static_cast<int>(hsl[0] * 100.0f);
        hsl[2] *= 1.0f + (sampleHueVsCurve(config.hueVsLumaCurve, hueIndex) * sat * 2.0f);
        if ( hsl[2] < 0.0f ) hsl[2] = 0.0f;
        hsl[1] *= 1.0f + (sampleHueVsCurve(config.hueVsSaturationCurve, hueIndex) * 2.0f);
        if ( hsl[1] < 0.0f ) hsl[1] = 0.0f;
        hsl[0] += 60.0f * sampleHueVsCurve(config.hueVsHueCurve, hueIndex);
        if ( hsl[0] < 0.0f ) hsl[0] += 360.0f;
        else if ( hsl[0] >= 360.0f ) hsl[0] -= 360.0f;
        const int lumaIndex = static_cast<int>(hsl[2] * 36000.0f);
        hsl[1] *= 1.0f + (sampleHueVsCurve(config.lumaVsSaturationCurve, lumaIndex) * 2.0f);
        if ( hsl[1] < 0.0f ) hsl[1] = 0.0f;

        previewFromHSVtoRGB(hsl, rgb);
        for (int channel = 0; channel < 3; ++channel)
        {
            const int rounded = static_cast<int>(rgb[channel] * 65535.0f + 0.5f);
            gammaOut[channel] = static_cast<uint16_t>(
                rounded < 0 ? 0 : (rounded > 65535 ? 65535 : rounded));
        }
    }

    if ( config.applyVibrance )
    {
        /* Vibrance, before saturation, matching raw_processing.c:3587-3644.
         * pix0 = trunc((pix-Y1)*vibrance) + Y1 (pre_calc_vibrance LUT reduced);
         * positive vibrance blends toward the original by the pixel's own
         * saturation, negative vibrance is just pix0. Float32 mirrors the shader. */
        const int32_t Y1 = ((static_cast<int32_t>(gammaOut[0]) << 2)
                          + (static_cast<int32_t>(gammaOut[1]) * 11)
                          + static_cast<int32_t>(gammaOut[2])) >> 4;
        int32_t pix0[3];
        for (int channel = 0; channel < 3; ++channel)
        {
            pix0[channel] = static_cast<int32_t>(std::trunc(
                (static_cast<float>(gammaOut[channel]) - static_cast<float>(Y1)) * config.vibrance)) + Y1;
        }
        if ( config.vibrance > 1.0f )
        {
            const int biggest = std::max(std::max(int(gammaOut[0]), int(gammaOut[1])), int(gammaOut[2]));
            const int smallest = std::min(std::min(int(gammaOut[0]), int(gammaOut[1])), int(gammaOut[2]));
            float sat = (biggest > 0)
                ? (static_cast<float>(biggest) - static_cast<float>(smallest)) / static_cast<float>(biggest)
                : 0.0f;
            sat = 2.0f * sat / (sat * sat + 1.0f);
            if ( sat > 1.0f ) sat = 1.0f;
            for (int channel = 0; channel < 3; ++channel)
            {
                const float blended = static_cast<float>(gammaOut[channel]) * sat
                                    + static_cast<float>(pix0[channel]) * (1.0f - sat);
                const int32_t b = static_cast<int32_t>(blended);
                gammaOut[channel] = static_cast<uint16_t>(b < 0 ? 0 : (b > 65535 ? 65535 : b));
            }
        }
        else
        {
            for (int channel = 0; channel < 3; ++channel)
            {
                gammaOut[channel] = static_cast<uint16_t>(
                    pix0[channel] < 0 ? 0 : (pix0[channel] > 65535 ? 65535 : pix0[channel]));
            }
        }
    }

    if ( config.applySaturation )
    {
        /* Cross-channel saturation, before toning, matching raw_processing.c
         * 3646-3671: Y1 = (4R+11G+B)>>4 integer luma; out = LIMIT16(trunc((pix-Y1)
         * *saturation) + Y1). The LUT pre_calc_sat[index]=trunc((index-65536)*sat)
         * with index=pix-Y1+65536 reduces to this direct form (float32, matching
         * the shader; +/-1 LSB vs the CPU double LUT, within engine tolerance). */
        const int32_t Y1 = ((static_cast<int32_t>(gammaOut[0]) << 2)
                          + (static_cast<int32_t>(gammaOut[1]) * 11)
                          + static_cast<int32_t>(gammaOut[2])) >> 4;
        for (int channel = 0; channel < 3; ++channel)
        {
            const float chroma = std::trunc(
                (static_cast<float>(gammaOut[channel]) - static_cast<float>(Y1)) * config.saturation);
            const int32_t saturated = static_cast<int32_t>(chroma) + Y1;
            gammaOut[channel] = static_cast<uint16_t>(
                saturated < 0 ? 0 : (saturated > 65535 ? 65535 : saturated));
        }
    }

    for (int channel = 0; channel < 3; ++channel)
    {
        uint16_t value = gammaOut[channel];
        if ( config.applyToning )
        {
            /* Per-channel scalar gain, before the contrast curve, matching
             * raw_processing.c:3685 (truncate to uint16, no clamp). */
            value = static_cast<uint16_t>(static_cast<float>(value) * config.toningGain[channel]);
        }
        if ( config.applyCreativeCurves )
        {
            /* Post-gamma creative curves, mirroring raw_processing.c:3696-3738:
             * contrast curve (pre_calc_curve_r) on all channels, then gradation
             * gcurve_y on all channels, then per-channel gcurve_r/g/b. */
            value = sampleLut(config.contrastCurveLut, value / 65535.0f);
            value = sampleLut(config.gradationLutY, value / 65535.0f);
            const QByteArray & perChannelGradation =
                (channel == 0) ? config.gradationLutR
              : (channel == 1) ? config.gradationLutG
                               : config.gradationLutB;
            value = sampleLut(perChannelGradation, value / 65535.0f);
        }
        outputPixel[channel] = value;
    }

    if ( config.applyAgx )
    {
        /* AgX inverse, at the very end after gamma + all creative stages
         * (raw_processing_8bit_kernel.inc:358-368): undo the compressed-gamut
         * matrix (double accumulation, float coeffs), then (uint16_t)LIMIT16 =
         * clamp+truncate. */
        const double f0 = static_cast<double>(outputPixel[0]);
        const double f1 = static_cast<double>(outputPixel[1]);
        const double f2 = static_cast<double>(outputPixel[2]);
        const double iv[3] = {
            f0 * config.agxInverse[0] + f1 * config.agxInverse[1] + f2 * config.agxInverse[2],
            f0 * config.agxInverse[3] + f1 * config.agxInverse[4] + f2 * config.agxInverse[5],
            f0 * config.agxInverse[6] + f1 * config.agxInverse[7] + f2 * config.agxInverse[8] };
        for (int channel = 0; channel < 3; ++channel)
        {
            const double clamped = iv[channel] < 0.0 ? 0.0 : (iv[channel] > 65535.0 ? 65535.0 : iv[channel]);
            outputPixel[channel] = static_cast<uint16_t>(clamped);
        }
    }

    if ( config.applyLut )
    {
        /* LUT (.cube) is the LAST stage (cube_lut.c apply_lut, run last at
         * raw_processing.c:3767). Domain-scaled index (note the 65536.0 divisor and
         * the domain_min post-subtract), then 1D per-channel lerp or 3D TETRAHEDRAL
         * interpolation (USE_TRILIN_INT is off in the engine), blended with the
         * pre-LUT pixel by lutIntensity. Negative indices floor to 0 (the engine's
         * (uint16_t) wrap differs only for domain_min>0 + dark pixels). */
        const float * cube = reinterpret_cast<const float *>(config.lutCube.constData());
        const int dim = config.lutDimension;
        const float fA = (dim - 1) / 65536.0f / (config.lutDomainMax[0] - config.lutDomainMin[0]);
        const float fB = (dim - 1) / 65536.0f / (config.lutDomainMax[1] - config.lutDomainMin[1]);
        const float fC = (dim - 1) / 65536.0f / (config.lutDomainMax[2] - config.lutDomainMin[2]);
        const float red   = outputPixel[0] * fA - config.lutDomainMin[0];
        const float green = outputPixel[1] * fB - config.lutDomainMin[1];
        const float blue  = outputPixel[2] * fC - config.lutDomainMin[2];
        int r0 = red   < 0.0f ? 0 : static_cast<int>(red);
        int g0 = green < 0.0f ? 0 : static_cast<int>(green);
        int b0 = blue  < 0.0f ? 0 : static_cast<int>(blue);
        int r1 = r0 + 1, g1 = g0 + 1, b1 = b0 + 1;
        if ( r0 >= dim ) r0 = dim - 1;
        if ( g0 >= dim ) g0 = dim - 1;
        if ( b0 >= dim ) b0 = dim - 1;
        if ( r1 >= dim ) r1 = dim - 1;
        if ( g1 >= dim ) g1 = dim - 1;
        if ( b1 >= dim ) b1 = dim - 1;
        const float f1 = config.lutIntensity;
        const float f2 = 1.0f - f1;
        float outC[3];
        if ( !config.lut3d )
        {
            outC[0] = lutLerp(red,   static_cast<float>(r0), static_cast<float>(r1), cube[r0 * 3 + 0], cube[r1 * 3 + 0]);
            outC[1] = lutLerp(green, static_cast<float>(g0), static_cast<float>(g1), cube[g0 * 3 + 1], cube[g1 * 3 + 1]);
            outC[2] = lutLerp(blue,  static_cast<float>(b0), static_cast<float>(b1), cube[b0 * 3 + 2], cube[b1 * 3 + 2]);
        }
        else
        {
            const int dim2 = dim * dim;
            const float rf = red - static_cast<float>(r0);
            const float gf = green - static_cast<float>(g0);
            const float bf = blue - static_cast<float>(b0);
            for (int i = 0; i < 3; ++i)
            {
                const float q000 = cube[((r0) + (g0) * dim + (b0) * dim2) * 3 + i];
                const float q001 = cube[((r0) + (g0) * dim + (b1) * dim2) * 3 + i];
                const float q010 = cube[((r0) + (g1) * dim + (b0) * dim2) * 3 + i];
                const float q011 = cube[((r0) + (g1) * dim + (b1) * dim2) * 3 + i];
                const float q100 = cube[((r1) + (g0) * dim + (b0) * dim2) * 3 + i];
                const float q101 = cube[((r1) + (g0) * dim + (b1) * dim2) * 3 + i];
                const float q110 = cube[((r1) + (g1) * dim + (b0) * dim2) * 3 + i];
                const float q111 = cube[((r1) + (g1) * dim + (b1) * dim2) * 3 + i];
                float o;
                if ( gf >= bf && bf >= rf )      o = (1.0f - gf) * q000 + (gf - bf) * q010 + (bf - rf) * q011 + rf * q111;
                else if ( bf > rf && rf > gf )   o = (1.0f - bf) * q000 + (bf - rf) * q001 + (rf - gf) * q101 + gf * q111;
                else if ( bf > gf && gf >= rf )  o = (1.0f - bf) * q000 + (bf - gf) * q001 + (gf - rf) * q011 + rf * q111;
                else if ( rf >= gf && gf > bf )  o = (1.0f - rf) * q000 + (rf - gf) * q100 + (gf - bf) * q110 + bf * q111;
                else if ( gf > rf && rf >= bf )  o = (1.0f - gf) * q000 + (gf - rf) * q010 + (rf - bf) * q110 + bf * q111;
                else                             o = (1.0f - rf) * q000 + (rf - bf) * q100 + (bf - gf) * q101 + gf * q111;
                outC[i] = o;
            }
        }
        for (int channel = 0; channel < 3; ++channel)
        {
            const float scaled = outC[channel] * 65535.0f;
            const float limited = scaled < 0.0f ? 0.0f : (scaled > 65535.0f ? 65535.0f : scaled);
            const float blended = static_cast<float>(outputPixel[channel]) * f2 + limited * f1;
            const int v = static_cast<int>(blended);
            outputPixel[channel] = static_cast<uint16_t>(v < 0 ? 0 : (v > 65535 ? 65535 : v));
        }
    }
}

QSurfaceFormat previewProcessingSurfaceFormat()
{
    QSurfaceFormat format;
    format.setRenderableType(QSurfaceFormat::OpenGL);
    format.setVersion(2, 0);
    format.setProfile(QSurfaceFormat::NoProfile);
    return format;
}

QString rendererDescriptionFromFunctions(QOpenGLFunctions * functions)
{
    if ( !functions ) return QStringLiteral("unknown");
    const GLubyte * renderer = functions->glGetString(GL_RENDERER);
    if ( !renderer ) return QStringLiteral("unknown");
    return QString::fromLatin1(reinterpret_cast<const char *>(renderer));
}

bool previewProcessingGlDispatchIsReady(QOpenGLContext * context,
                                        QOpenGLFunctions * functions,
                                        QString * reason)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( !context || !context->isValid()
      || QOpenGLContext::currentContext() != context )
    {
        return fail(QStringLiteral("offscreen preview GL context is not current and valid"));
    }
    if ( !functions )
    {
        return fail(QStringLiteral("offscreen preview GL dispatch object is null"));
    }

    /* QOpenGLFunctions is only a wrapper. On a broken/headless platform it can
     * exist while one of the entry points needed by this path is unavailable.
     * Check the dispatch table before any wrapper call that could otherwise
     * become a null control transfer (the hosted CI-1 failure was Rip=0). */
    static constexpr const char * requiredFunctions[] = {
        "glGetString", "glActiveTexture", "glBindTexture", "glTexParameteri",
        "glDrawArrays", "glReadPixels", "glFinish", "glViewport",
        "glClear", "glClearColor", "glDisable", "glGenTextures",
        "glDeleteTextures", "glGenFramebuffers", "glBindFramebuffer",
        "glFramebufferTexture2D", "glCheckFramebufferStatus"
    };
    for (const char * name : requiredFunctions)
    {
        if ( !context->getProcAddress(name) )
        {
            return fail(QStringLiteral("offscreen preview GL dispatch unavailable: %1")
                            .arg(QString::fromLatin1(name)));
        }
    }
    return true;
}

bool previewProcessingTextureIsReady(const QOpenGLTexture * texture)
{
    return texture && texture->isCreated() && texture->isStorageAllocated()
        && texture->textureId() != 0;
}

QOpenGLTexture * createLookupTexture()
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
    texture->setSize(kLutTextureEdge, kLutTextureEdge);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

/* hue-vs / luma-vs curves are signed float[36000] in [-1,1]; an R32F texture
 * holds them exactly (the uint16 LUT path would quantize to ~3e-5 and break
 * bit-alignment with the CPU reference and the production float curves).
 * 256 * 141 = 36096 >= 36000 entries, sampled by integer index. */
constexpr int kHueVsCurveTextureWidth = 256;
constexpr int kHueVsCurveTextureHeight = 141;

QOpenGLTexture * createCurveTexture()
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::R32F);
    texture->setSize(kHueVsCurveTextureWidth, kHueVsCurveTextureHeight);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

QByteArray packCurveTextureR32F(const QByteArray & curveBytes)
{
    QByteArray packed(kHueVsCurveTextureWidth * kHueVsCurveTextureHeight
                      * static_cast<int>(sizeof(float)), Qt::Uninitialized);
    std::memset(packed.data(), 0, static_cast<size_t>(packed.size()));
    const int wanted = static_cast<int>(kHueVsCurveSamples * sizeof(float));
    if ( curveBytes.size() >= wanted )
    {
        std::memcpy(packed.data(), curveBytes.constData(), static_cast<size_t>(wanted));
    }
    return packed;
}

/* The in-loop contrast curve has 65536 entries (16-bit luma index) = 256*256,
 * so it fills a full R32F lookup texture. A neutral (empty) curve packs to all
 * 1.0 so the multiply is inert. */
QOpenGLTexture * createContrastCurveTexture()
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::R32F);
    texture->setSize(kLutTextureEdge, kLutTextureEdge);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

/* The vignette mask is a full-frame R32F texture (one texel per source pixel,
 * raster-ordered to match the CPU debayered buffer). Dynamic width x height,
 * unlike the fixed-size LUT textures. */
QOpenGLTexture * createVignetteMaskTexture(int width, int height)
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::R32F);
    texture->setSize(width, height);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

/* 3D LUT cube as a dim x dim x dim RGBA32F volume texture (Target3D, core since
 * GL 1.2 / sampler3D in GLSL 110). The cube's flat index r + g*dim + b*dim^2 maps
 * to texel (r,g,b) row-major, so the shader samples the 8 corners directly. */
QOpenGLTexture * createLut3dTexture(int dim)
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target3D);
    texture->setFormat(QOpenGLTexture::RGBA32F);
    texture->setSize(dim, dim, dim);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

/* 1D LUT as a dim x 1 RGBA32F texture (one texel per cube entry). */
QOpenGLTexture * createLut1dTexture(int dim)
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA32F);
    texture->setSize(dim, 1);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

/* Pack the raw RGB float cube (3 floats/entry) into RGBA32F (A=0) for upload. */
QByteArray packLutCubeRgba32F(const QByteArray & cubeBytes, int entries)
{
    QByteArray packed(entries * 4 * static_cast<int>(sizeof(float)), Qt::Uninitialized);
    float * dst = reinterpret_cast<float *>(packed.data());
    const float * src = reinterpret_cast<const float *>(cubeBytes.constData());
    const int srcFloats = cubeBytes.size() / static_cast<int>(sizeof(float));
    for (int e = 0; e < entries; ++e)
    {
        dst[e * 4 + 0] = (e * 3 + 0 < srcFloats) ? src[e * 3 + 0] : 0.0f;
        dst[e * 4 + 1] = (e * 3 + 1 < srcFloats) ? src[e * 3 + 1] : 0.0f;
        dst[e * 4 + 2] = (e * 3 + 2 < srcFloats) ? src[e * 3 + 2] : 0.0f;
        dst[e * 4 + 3] = 0.0f;
    }
    return packed;
}

QByteArray packContrastCurveR32F(const QByteArray & curveBytes)
{
    const int count = kLutTextureEdge * kLutTextureEdge;
    QByteArray packed(count * static_cast<int>(sizeof(float)), Qt::Uninitialized);
    float * dst = reinterpret_cast<float *>(packed.data());
    const int wanted = static_cast<int>(kInLoopContrastSamples * sizeof(float));
    if ( curveBytes.size() >= wanted )
    {
        std::memcpy(packed.data(), curveBytes.constData(), static_cast<size_t>(wanted));
    }
    else
    {
        for (int index = 0; index < count; ++index) dst[index] = 1.0f;
    }
    return packed;
}

QOpenGLTexture * createFrameTexture(int width, int height)
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
    texture->setSize(width, height);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

QByteArray packRgb16Texture(const uint16_t * inputRgb16, int pixelCount)
{
    QByteArray packed(static_cast<int>(pixelCount * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    uint16_t * packedValues = reinterpret_cast<uint16_t *>(packed.data());
    for (int pixelIndex = 0; pixelIndex < pixelCount; ++pixelIndex)
    {
        packedValues[pixelIndex * 4 + 0] = inputRgb16[pixelIndex * 3 + 0];
        packedValues[pixelIndex * 4 + 1] = inputRgb16[pixelIndex * 3 + 1];
        packedValues[pixelIndex * 4 + 2] = inputRgb16[pixelIndex * 3 + 2];
        packedValues[pixelIndex * 4 + 3] = 65535;
    }
    return packed;
}

void unpackRgb16Readback(const QByteArray & readbackRgba16,
                         uint16_t * outputRgb16,
                         int width,
                         int height)
{
    const uint16_t * pixels = reinterpret_cast<const uint16_t *>(readbackRgba16.constData());
    for (int y = 0; y < height; ++y)
    {
        const int sourceY = height - 1 - y;
        for (int x = 0; x < width; ++x)
        {
            const int sourceIndex = (sourceY * width + x) * 4;
            const int destIndex = (y * width + x) * 3;
            outputRgb16[destIndex + 0] = pixels[sourceIndex + 0];
            outputRgb16[destIndex + 1] = pixels[sourceIndex + 1];
            outputRgb16[destIndex + 2] = pixels[sourceIndex + 2];
        }
    }
}

bool buildSubsetProgram(QOpenGLShaderProgram * program, QString * reason)
{
    if ( !program ) return false;

    const QByteArray vertexShader = gpuPreviewProcessingVertexShaderSource();
    const QByteArray fragmentShader = gpuPreviewProcessingSubsetFragmentShaderSource();
    if ( !program->addShaderFromSourceCode(QOpenGLShader::Vertex, vertexShader)
      || !program->addShaderFromSourceCode(QOpenGLShader::Fragment, fragmentShader)
      || !program->link() )
    {
        if ( reason )
        {
            *reason = QStringLiteral("QOpenGLShaderProgram preview-processing subset setup failed: %1")
                .arg(program->log());
        }
        return false;
    }
    return true;
}

bool makePreviewProcessingContextCurrent(QOffscreenSurface * surface,
                                         QOpenGLContext * context,
                                         QOpenGLFunctions ** functions,
                                         QString * reason,
                                         QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription && rendererDescription->isEmpty() )
        {
            *rendererDescription = QStringLiteral("unknown");
        }
        return false;
    };

    if ( !qobject_cast<QGuiApplication *>(QCoreApplication::instance()) )
    {
        return fail(QStringLiteral("QOffscreenSurface requires a QGuiApplication instance"));
    }

    if ( !surface || !context ) return fail(QStringLiteral("QOffscreenSurface setup received null objects"));

    const QSurfaceFormat format = previewProcessingSurfaceFormat();
    surface->setFormat(format);
    surface->create();
    if ( !surface->isValid() )
    {
        return fail(QStringLiteral("QOffscreenSurface creation failed"));
    }

    context->setFormat(surface->requestedFormat());
    if ( !context->create() )
    {
        return fail(QStringLiteral("QOpenGLContext creation failed"));
    }

    if ( !context->makeCurrent(surface) )
    {
        return fail(QStringLiteral("QOffscreenSurface makeCurrent failed"));
    }

    QOpenGLFunctions * glFunctions = context->functions();
    if ( !glFunctions )
    {
        context->doneCurrent();
        return fail(QStringLiteral("QOpenGLContext did not expose QOpenGLFunctions"));
    }

    if ( !previewProcessingGlDispatchIsReady(context, glFunctions, reason) )
    {
        context->doneCurrent();
        return fail(reason ? *reason
                           : QStringLiteral("offscreen preview GL dispatch unavailable"));
    }

    const QString renderer = rendererDescriptionFromFunctions(glFunctions);
    if ( rendererDescription ) *rendererDescription = renderer;
    if ( functions ) *functions = glFunctions;

    /* GPU preview is hardware-only in production: a software rasterizer (llvmpipe,
     * WARP, ...) is slower than the CPU path and offers no benefit, so it is
     * rejected and the caller falls back to CPU. The MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE
     * diagnostic escape hatch (off by default) lets the CPU-vs-GPU parity harness
     * validate shader *logic* on a software GL backend in headless CI, where no
     * hardware GPU is available; it never changes default/production behaviour. */
    if ( gpuPreviewProcessingRendererIsSoftware(renderer)
      && !envFlagEnabled(qgetenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE")) )
    {
        context->doneCurrent();
        return fail(QStringLiteral("software rasterizer renderer: %1").arg(renderer));
    }

    return true;
}
}

const char * gpuPreviewProcessingEnvironmentVariableName(void)
{
    return "MLVAPP_EXPERIMENTAL_GPU_PROCESSING";
}

bool gpuPreviewProcessingRequestedByEnvironment(void)
{
    return envFlagEnabled(qgetenv(gpuPreviewProcessingEnvironmentVariableName()));
}

QByteArray gpuPreviewProcessingVertexShaderSource(void)
{
    return QByteArrayLiteral(
        "attribute vec2 position;\n"
        "attribute vec2 texCoord;\n"
        "varying vec2 vTexCoord;\n"
        "void main()\n"
        "{\n"
        "    gl_Position = vec4(position, 0.0, 1.0);\n"
        "    vTexCoord = texCoord;\n"
        "}\n");
}

QByteArray gpuPreviewProcessingDisplayFragmentShaderSource(void)
{
    return QByteArrayLiteral(
        "uniform sampler2D frameTexture;\n"
        "uniform sampler2D levelsLut;\n"
        "uniform sampler2D matrixLutR;\n"
        "uniform sampler2D matrixLutG;\n"
        "uniform sampler2D matrixLutB;\n"
        "uniform sampler2D gammaLut;\n"
        "uniform vec2 textureSize;\n"
        "uniform int frameTextureMode;\n"
        "uniform int samplingMode;\n"
        "uniform float zebraEnabled;\n"
        "uniform float zebraUnderThreshold;\n"
        "uniform float zebraOverThreshold;\n"
        "uniform float previewProcessingEnabled;\n"
        "uniform float previewUseCameraMatrix;\n"
        "uniform float previewApplyGamutCompression;\n"
        "uniform vec3 previewProperWbRow0;\n"
        "uniform vec3 previewProperWbRow1;\n"
        "uniform vec3 previewProperWbRow2;\n"
        "uniform vec3 previewRgbToY;\n"
        "varying vec2 vTexCoord;\n"
        "float cubicWeight(float x)\n"
        "{\n"
        "    x = abs(x);\n"
        "    if (x <= 1.0)\n"
        "    {\n"
        "        return ((1.5 * x - 2.5) * x * x) + 1.0;\n"
        "    }\n"
        "    if (x < 2.0)\n"
        "    {\n"
        "        return (((-0.5 * x + 2.5) * x) - 4.0) * x + 2.0;\n"
        "    }\n"
        "    return 0.0;\n"
        "}\n"
        "vec4 sampleBicubic(vec2 uv)\n"
        "{\n"
        "    vec2 coord = uv * textureSize - 0.5;\n"
        "    vec2 base = floor(coord);\n"
        "    vec2 f = coord - base;\n"
        "    vec4 sum = vec4(0.0);\n"
        "    float totalWeight = 0.0;\n"
        "    for (int j = -1; j <= 2; ++j)\n"
        "    {\n"
        "        for (int i = -1; i <= 2; ++i)\n"
        "        {\n"
        "            float wx = cubicWeight(float(i) - f.x);\n"
        "            float wy = cubicWeight(float(j) - f.y);\n"
        "            float w = wx * wy;\n"
        "            vec2 sampleCoord = (base + vec2(float(i), float(j)) + 0.5) / textureSize;\n"
        "            sum += texture2D(frameTexture, sampleCoord) * w;\n"
        "            totalWeight += w;\n"
        "        }\n"
        "    }\n"
        "    if (totalWeight <= 0.0)\n"
        "    {\n"
        "        return texture2D(frameTexture, uv);\n"
        "    }\n"
        "    return sum / totalWeight;\n"
        "}\n"
        "float sampleBayerRaw(vec2 logicalPixel)\n"
        "{\n"
        "    vec2 clamped = clamp(logicalPixel, vec2(0.0), textureSize - vec2(1.0));\n"
        "    vec2 uv = vec2(clamped.x + 0.5, clamped.y + 0.5) / textureSize;\n"
        "    return texture2D(frameTexture, uv).r;\n"
        "}\n"
        "vec3 debayerBilinearRggb(vec2 outputPixel)\n"
        "{\n"
        "    vec2 corePixel = clamp(outputPixel, vec2(1.0), textureSize - vec2(2.0));\n"
        "    float xParity = mod(corePixel.x, 2.0);\n"
        "    float yParity = mod(corePixel.y, 2.0);\n"
        "    float raw = sampleBayerRaw(corePixel);\n"
        "    float up = sampleBayerRaw(corePixel + vec2(0.0, -1.0));\n"
        "    float down = sampleBayerRaw(corePixel + vec2(0.0, 1.0));\n"
        "    float left = sampleBayerRaw(corePixel + vec2(-1.0, 0.0));\n"
        "    float right = sampleBayerRaw(corePixel + vec2(1.0, 0.0));\n"
        "    float upLeft = sampleBayerRaw(corePixel + vec2(-1.0, -1.0));\n"
        "    float upRight = sampleBayerRaw(corePixel + vec2(1.0, -1.0));\n"
        "    float downLeft = sampleBayerRaw(corePixel + vec2(-1.0, 1.0));\n"
        "    float downRight = sampleBayerRaw(corePixel + vec2(1.0, 1.0));\n"
        "    if (xParity < 0.5 && yParity < 0.5)\n"
        "    {\n"
        "        return vec3(raw,\n"
        "                    (up + down + left + right) * 0.25,\n"
        "                    (upLeft + upRight + downLeft + downRight) * 0.25);\n"
        "    }\n"
        "    if (xParity > 0.5 && yParity > 0.5)\n"
        "    {\n"
        "        return vec3((upLeft + upRight + downLeft + downRight) * 0.25,\n"
        "                    (up + down + left + right) * 0.25,\n"
        "                    raw);\n"
        "    }\n"
        "    if (xParity > 0.5)\n"
        "    {\n"
        "        return vec3((left + right) * 0.5,\n"
        "                    raw,\n"
        "                    (up + down) * 0.5);\n"
        "    }\n"
        "    return vec3((up + down) * 0.5,\n"
        "                raw,\n"
        "                (left + right) * 0.5);\n"
        "}\n"
        "vec4 sampleFrame(vec2 uv)\n"
        "{\n"
        "    if (frameTextureMode == 1)\n"
        "    {\n"
        "        vec2 outputPixel = floor(vec2(uv.x * textureSize.x,\n"
        "                                      (1.0 - uv.y) * textureSize.y));\n"
        "        return vec4(debayerBilinearRggb(outputPixel), 1.0);\n"
        "    }\n"
        "    if (samplingMode == 2)\n"
        "    {\n"
        "        return sampleBicubic(uv);\n"
        "    }\n"
        "    return texture2D(frameTexture, uv);\n"
        "}\n"
        "float sampleU16Lut(sampler2D lut, float value)\n"
        "{\n"
        "    float clamped = clamp(value, 0.0, 1.0);\n"
        "    float index = floor(clamped * 65535.0 + 0.5);\n"
        "    float x = mod(index, 256.0);\n"
        "    float y = floor(index / 256.0);\n"
        "    vec2 uv = (vec2(x, y) + vec2(0.5)) / vec2(256.0, 256.0);\n"
        "    return texture2D(lut, uv).r;\n"
        "}\n"
        "float reinhardTonemap(float x)\n"
        "{\n"
        "    return (x < 0.0) ? x : x / (1.0 + x);\n"
        "}\n"
        "float reinhardForColour(float x)\n"
        "{\n"
        "    return (x < 0.5) ? x : (reinhardTonemap((x - 0.5) / 0.5) * 0.5 + 0.5);\n"
        "}\n"
        "float reinhardForBlue(float x)\n"
        "{\n"
        "    return (x < 0.7) ? x : (reinhardTonemap((x - 0.7) / 0.3) * 0.3 + 0.7);\n"
        "}\n"
        "vec3 applyPreviewProcessing(vec3 color)\n"
        "{\n"
        "    if (previewProcessingEnabled <= 0.5)\n"
        "    {\n"
        "        return color;\n"
        "    }\n"
        "    vec3 leveled = vec3(sampleU16Lut(levelsLut, color.r), sampleU16Lut(levelsLut, color.g), sampleU16Lut(levelsLut, color.b));\n"
        "    vec3 matrixApplied = vec3(sampleU16Lut(matrixLutR, leveled.r), sampleU16Lut(matrixLutG, leveled.g), sampleU16Lut(matrixLutB, leveled.b));\n"
        "    if (previewUseCameraMatrix > 0.5)\n"
        "    {\n"
        "        vec3 wbApplied = vec3(dot(previewProperWbRow0, matrixApplied), dot(previewProperWbRow1, matrixApplied), dot(previewProperWbRow2, matrixApplied));\n"
        "        if (previewApplyGamutCompression > 0.5)\n"
        "        {\n"
        "            float Y = dot(previewRgbToY, wbApplied);\n"
        "            float minChannel = min(min(wbApplied.r, wbApplied.g), wbApplied.b);\n"
        "            vec3 gamutReference = vec3(-(reinhardForColour((Y != 0.0) ? ((Y - wbApplied.r) / Y) : 0.0) * Y) + Y,\n"
        "                                      -(reinhardForBlue((Y != 0.0) ? ((Y - wbApplied.g) / Y) : 0.0) * Y) + Y,\n"
        "                                      -(reinhardForBlue((Y != 0.0) ? ((Y - wbApplied.b) / Y) : 0.0) * Y) + Y);\n"
        "            float gamutMin = min(min(gamutReference.r, gamutReference.g), gamutReference.b);\n"
        "            float desaturateFactor = 1.0;\n"
        "            float denom = Y - minChannel;\n"
        "            if (Y > 0.0 && abs(denom) > 0.00000001)\n"
        "            {\n"
        "                desaturateFactor = (Y - gamutMin) / denom;\n"
        "            }\n"
        "            wbApplied = (wbApplied - vec3(Y)) * desaturateFactor + vec3(Y);\n"
        "        }\n"
        "        matrixApplied = wbApplied;\n"
        "    }\n"
        "    matrixApplied = clamp(matrixApplied, 0.0, 1.0);\n"
        "    return vec3(sampleU16Lut(gammaLut, matrixApplied.r), sampleU16Lut(gammaLut, matrixApplied.g), sampleU16Lut(gammaLut, matrixApplied.b));\n"
        "}\n"
        "vec4 applyDisplayProcessing(vec4 color)\n"
        "{\n"
        "    color.rgb = applyPreviewProcessing(color.rgb);\n"
        "    if (zebraEnabled > 0.5)\n"
        "    {\n"
        "        float maxChannel = max(max(color.r, color.g), color.b);\n"
        "        float minChannel = min(min(color.r, color.g), color.b);\n"
        "        float lightness = (maxChannel + minChannel) * 0.5;\n"
        "        if (lightness >= zebraOverThreshold)\n"
        "        {\n"
        "            return vec4(1.0, 0.0, 0.0, color.a);\n"
        "        }\n"
        "        if (lightness <= zebraUnderThreshold)\n"
        "        {\n"
        "            return vec4(0.0, 0.0, 1.0, color.a);\n"
        "        }\n"
        "    }\n"
        "    return color;\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec4 sampledColor = sampleFrame(vTexCoord);\n"
        "    gl_FragColor = applyDisplayProcessing(sampledColor);\n"
        "}\n");
}

QByteArray gpuPreviewProcessingSubsetFragmentShaderSource(void)
{
    return QByteArrayLiteral(
        "uniform sampler2D frameTexture;\n"
        "uniform sampler2D levelsLut;\n"
        "uniform sampler2D matrixLutR;\n"
        "uniform sampler2D matrixLutG;\n"
        "uniform sampler2D matrixLutB;\n"
        "uniform sampler2D gammaLut;\n"
        "uniform sampler2D contrastCurveLut;\n"
        "uniform sampler2D gradationLutY;\n"
        "uniform sampler2D gradationLutR;\n"
        "uniform sampler2D gradationLutG;\n"
        "uniform sampler2D gradationLutB;\n"
        "uniform sampler2D hueVsHueCurve;\n"
        "uniform sampler2D hueVsSaturationCurve;\n"
        "uniform sampler2D hueVsLumaCurve;\n"
        "uniform sampler2D lumaVsSaturationCurve;\n"
        "uniform sampler2D inLoopContrastCurve;\n"
        "uniform sampler2D shadowsHighlightsBlurTexture;\n"
        "uniform sampler2D shadowsHighlightsCurve;\n"
        "uniform float previewProcessingEnabled;\n"
        "uniform float previewApplyCreativeCurves;\n"
        "uniform float previewApplyToning;\n"
        "uniform vec3 previewToningGain;\n"
        "uniform float previewApplyVibrance;\n"
        "uniform float previewVibrance;\n"
        "uniform float previewApplySaturation;\n"
        "uniform float previewSaturation;\n"
        "uniform float previewApplyHueVs;\n"
        "uniform float previewApplyInLoopContrast;\n"
        "uniform float previewApplyShadowsHighlights;\n"
        "uniform float previewShadowsHighlightsCurveIndexMask;\n"
        "uniform float previewApplyAgx;\n"
        "uniform vec3 previewAgxFwd0;\n"
        "uniform vec3 previewAgxFwd1;\n"
        "uniform vec3 previewAgxFwd2;\n"
        "uniform vec3 previewAgxInv0;\n"
        "uniform vec3 previewAgxInv1;\n"
        "uniform vec3 previewAgxInv2;\n"
        "uniform sampler2D vignetteMask;\n"
        "uniform float previewApplyVignette;\n"
        "uniform float previewVignetteStrength;\n"
        "uniform vec2 frameSize;\n"
        "uniform sampler3D lut3dTexture;\n"
        "uniform sampler2D lut1dTexture;\n"
        "uniform float previewApplyLut;\n"
        "uniform float previewLut3d;\n"
        "uniform float previewLutDimension;\n"
        "uniform float previewLutIntensity;\n"
        "uniform vec3 previewLutDomainMin;\n"
        "uniform vec3 previewLutDomainMax;\n"
        "uniform float previewUseCameraMatrix;\n"
        "uniform float previewApplyGamutCompression;\n"
        "uniform vec3 previewProperWbRow0;\n"
        "uniform vec3 previewProperWbRow1;\n"
        "uniform vec3 previewProperWbRow2;\n"
        "uniform vec3 previewRgbToY;\n"
        "uniform float previewApplyHighlightRecon;\n"
        "uniform float previewHighlightReconDualIso;\n"
        "uniform float previewHighestGreen;\n"
        "uniform float previewHighestGreenDiso;\n"
        "uniform sampler2D gradMatrixLutR;\n"
        "uniform sampler2D gradMatrixLutG;\n"
        "uniform sampler2D gradMatrixLutB;\n"
        "uniform sampler2D gradGammaLut;\n"
        "uniform sampler2D gradientContrastCurve;\n"
        "uniform sampler2D gradientMask;\n"
        "uniform float previewApplyGradient;\n"
        "uniform float previewApplyGradientContrast;\n"
        "uniform float previewGradientHighestGreen;\n"
        "uniform float previewGradientHighestGreenDiso;\n"
        "varying vec2 vTexCoord;\n"
        "float sampleU16Lut(sampler2D lut, float value)\n"
        "{\n"
        "    float clamped = clamp(value, 0.0, 1.0);\n"
        "    float index = floor(clamped * 65535.0 + 0.5);\n"
        "    float x = mod(index, 256.0);\n"
        "    float y = floor(index / 256.0);\n"
        "    vec2 uv = (vec2(x, y) + vec2(0.5)) / vec2(256.0, 256.0);\n"
        "    return texture2D(lut, uv).r;\n"
        "}\n"
        "float sampleU16LutTrunc(sampler2D lut, float value)\n"
        "{\n"
        "    float clamped = clamp(value, 0.0, 1.0);\n"
        "    float index = floor(clamped * 65535.0);\n"
        "    float x = mod(index, 256.0);\n"
        "    float y = floor(index / 256.0);\n"
        "    vec2 uv = (vec2(x, y) + vec2(0.5)) / vec2(256.0, 256.0);\n"
        "    return texture2D(lut, uv).r;\n"
        "}\n"
        "float sampleHueVsCurve(sampler2D curve, float idx)\n"
        "{\n"
        "    float i = clamp(idx, 0.0, 35999.0);\n"
        "    float x = mod(i, 256.0);\n"
        "    float y = floor(i / 256.0);\n"
        "    vec2 uv = (vec2(x, y) + vec2(0.5)) / vec2(256.0, 141.0);\n"
        "    return texture2D(curve, uv).r;\n"
        "}\n"
        "float sampleContrastCurve(sampler2D curve, float idx)\n"
        "{\n"
        "    float i = clamp(idx, 0.0, 65535.0);\n"
        "    float x = mod(i, 256.0);\n"
        "    float y = floor(i / 256.0);\n"
        "    vec2 uv = (vec2(x, y) + vec2(0.5)) / vec2(256.0, 256.0);\n"
        "    return texture2D(curve, uv).r;\n"
        "}\n"
        "vec3 truncToZero(vec3 v)\n"
        "{\n"
        "    return sign(v) * floor(abs(v));\n"
        "}\n"
        "float lutLerp1d(float x, float x1, float x2, float q00, float q01)\n"
        "{\n"
        "    if (x2 - x1 == 0.0) return q00;\n"
        "    return ((x2 - x) / (x2 - x1)) * q00 + ((x - x1) / (x2 - x1)) * q01;\n"
        "}\n"
        "vec3 previewFromRGBtoHSV(vec3 rgb)\n"
        "{\n"
        "    float V = max(rgb.r, max(rgb.g, rgb.b));\n"
        "    float delta = V - min(rgb.r, min(rgb.g, rgb.b));\n"
        "    float H = 0.0;\n"
        "    float S = 0.0;\n"
        "    if (delta >= 1.17549435e-38)\n"
        "    {\n"
        "        S = delta / V;\n"
        "        if (rgb.r >= V)\n"
        "        {\n"
        "            H = (rgb.g - rgb.b) / delta;\n"
        "            if (H < 0.0) H += 6.0;\n"
        "        }\n"
        "        else if (rgb.g >= V)\n"
        "        {\n"
        "            H = 2.0 + (rgb.b - rgb.r) / delta;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            H = 4.0 + (rgb.r - rgb.g) / delta;\n"
        "        }\n"
        "    }\n"
        "    return vec3(H * 60.0, S, V);\n"
        "}\n"
        "vec3 previewFromHSVtoRGB(vec3 hsv)\n"
        "{\n"
        "    if (hsv.g < 1.17549435e-38)\n"
        "    {\n"
        "        return vec3(hsv.b);\n"
        "    }\n"
        "    float h = hsv.x / 60.0;\n"
        "    float fi = floor(h);\n"
        "    int i = int(fi);\n"
        "    float f = h - fi;\n"
        "    float p = hsv.z * (1.0 - hsv.y);\n"
        "    if (mod(fi, 2.0) >= 0.5)\n"
        "    {\n"
        "        float q = hsv.z * (1.0 - (hsv.y * f));\n"
        "        if (i == 1) return vec3(q, hsv.z, p);\n"
        "        else if (i == 3) return vec3(p, q, hsv.z);\n"
        "        return vec3(hsv.z, p, q);\n"
        "    }\n"
        "    float t = hsv.z * (1.0 - (hsv.y * (1.0 - f)));\n"
        "    if (i == 0) return vec3(hsv.z, t, p);\n"
        "    else if (i == 2) return vec3(p, hsv.z, t);\n"
        "    return vec3(t, p, hsv.z);\n"
        "}\n"
        "float reinhardTonemap(float x)\n"
        "{\n"
        "    return (x < 0.0) ? x : x / (1.0 + x);\n"
        "}\n"
        "float reinhardForColour(float x)\n"
        "{\n"
        "    return (x < 0.5) ? x : (reinhardTonemap((x - 0.5) / 0.5) * 0.5 + 0.5);\n"
        "}\n"
        "float reinhardForBlue(float x)\n"
        "{\n"
        "    return (x < 0.7) ? x : (reinhardTonemap((x - 0.7) / 0.3) * 0.3 + 0.7);\n"
        "}\n"
        "vec3 applyPreviewProcessing(vec3 color)\n"
        "{\n"
        "    if (previewProcessingEnabled <= 0.5)\n"
        "    {\n"
        "        return color;\n"
        "    }\n"
        "    vec3 leveled = vec3(sampleU16Lut(levelsLut, color.r), sampleU16Lut(levelsLut, color.g), sampleU16Lut(levelsLut, color.b));\n"
        "    vec3 matrixApplied = vec3(sampleU16Lut(matrixLutR, leveled.r), sampleU16Lut(matrixLutG, leveled.g), sampleU16Lut(matrixLutB, leveled.b));\n"
        "    float reconMatrixGreen = matrixApplied.g;\n"
        "    float shadowsHighlightsF = 1.0;\n"
        "    if (previewApplyVignette > 0.5)\n"
        "    {\n"
        "        vec2 fc = vec2(vTexCoord.x, 1.0 - vTexCoord.y) * frameSize;\n"
        "        float idx = floor(fc.y) * frameSize.x + floor(fc.x) + 1.0;\n"
        "        if (idx < frameSize.x * frameSize.y)\n"
        "        {\n"
        "            float mx = mod(idx, frameSize.x);\n"
        "            float my = floor(idx / frameSize.x);\n"
        "            float m = texture2D(vignetteMask, (vec2(mx, my) + vec2(0.5)) / frameSize).r;\n"
        "            float base = 1.0 + (m * previewVignetteStrength / 128.0);\n"
        "            float b2 = base * base;\n"
        "            matrixApplied *= b2 * b2;\n"
        "        }\n"
        "    }\n"
        "    if (previewApplyShadowsHighlights > 0.5)\n"
        "    {\n"
        "        vec2 shfc = vec2(vTexCoord.x, 1.0 - vTexCoord.y) * frameSize;\n"
        "        vec3 shBlur = floor(texture2D(shadowsHighlightsBlurTexture, (floor(shfc) + vec2(0.5)) / frameSize).rgb * 65535.0 + 0.5);\n"
        "        float shBval = shBlur.r;\n"
        "        if (previewShadowsHighlightsCurveIndexMask <= 0.5)\n"
        "        {\n"
        "            float shR = floor(sampleU16Lut(matrixLutR, shBlur.r / 65535.0) * 65535.0 + 0.5);\n"
        "            float shG = floor(sampleU16Lut(matrixLutG, shBlur.g / 65535.0) * 65535.0 + 0.5);\n"
        "            float shB = floor(sampleU16Lut(matrixLutB, shBlur.b / 65535.0) * 65535.0 + 0.5);\n"
        "            shBval = floor((shR * 4.0 + shG * 11.0 + shB) / 16.0);\n"
        "        }\n"
        "        shadowsHighlightsF = sampleContrastCurve(shadowsHighlightsCurve, shBval);\n"
        "    }\n"
        "    if (previewApplyInLoopContrast > 0.5)\n"
        "    {\n"
        "        vec3 m16 = floor(matrixApplied * 65535.0 + 0.5);\n"
        "        float cval = floor((m16.r * 4.0 + m16.g * 11.0 + m16.b) / 16.0);\n"
        "        matrixApplied *= sampleContrastCurve(inLoopContrastCurve, cval);\n"
        "    }\n"
        "    if (previewApplyShadowsHighlights > 0.5)\n"
        "    {\n"
        "        matrixApplied *= shadowsHighlightsF;\n"
        "    }\n"
        "    if (previewApplyHighlightRecon > 0.5)\n"
        "    {\n"
        "        float p0 = floor(clamp(matrixApplied.r * 65535.0, 0.0, 65535.0));\n"
        "        float p1 = floor(clamp(matrixApplied.g * 65535.0, 0.0, 65535.0));\n"
        "        float p2 = floor(clamp(matrixApplied.b * 65535.0, 0.0, 65535.0));\n"
        "        float tmp1 = floor(clamp(reconMatrixGreen * 65535.0 + 0.5, 0.0, 65535.0));\n"
        "        bool replace = false;\n"
        "        if (previewHighlightReconDualIso > 0.5)\n"
        "        {\n"
        "            float lo = clamp(previewHighestGreenDiso - 5000.0, 0.0, 65535.0);\n"
        "            float hi = clamp(previewHighestGreenDiso + 5000.0, 0.0, 65535.0);\n"
        "            if (tmp1 >= lo && tmp1 <= hi && p1 < 1.1 * p0 && p1 < p2) replace = true;\n"
        "        }\n"
        "        else if (tmp1 == previewHighestGreen)\n"
        "        {\n"
        "            replace = true;\n"
        "        }\n"
        "        if (replace) matrixApplied.g = floor((p0 + p2) / 2.0) / 65535.0;\n"
        "    }\n"
        "    if (previewUseCameraMatrix > 0.5)\n"
        "    {\n"
        "        vec3 wbApplied = vec3(dot(previewProperWbRow0, matrixApplied), dot(previewProperWbRow1, matrixApplied), dot(previewProperWbRow2, matrixApplied));\n"
        "        if (previewApplyGamutCompression > 0.5)\n"
        "        {\n"
        "            float Y = dot(previewRgbToY, wbApplied);\n"
        "            float minChannel = min(min(wbApplied.r, wbApplied.g), wbApplied.b);\n"
        "            vec3 gamutReference = vec3(-(reinhardForColour((Y != 0.0) ? ((Y - wbApplied.r) / Y) : 0.0) * Y) + Y,\n"
        "                                      -(reinhardForBlue((Y != 0.0) ? ((Y - wbApplied.g) / Y) : 0.0) * Y) + Y,\n"
        "                                      -(reinhardForBlue((Y != 0.0) ? ((Y - wbApplied.b) / Y) : 0.0) * Y) + Y);\n"
        "            float gamutMin = min(min(gamutReference.r, gamutReference.g), gamutReference.b);\n"
        "            float desaturateFactor = 1.0;\n"
        "            float denom = Y - minChannel;\n"
        "            if (Y > 0.0 && abs(denom) > 0.00000001)\n"
        "            {\n"
        "                desaturateFactor = (Y - gamutMin) / denom;\n"
        "            }\n"
        "            wbApplied = (wbApplied - vec3(Y)) * desaturateFactor + vec3(Y);\n"
        "        }\n"
        "        matrixApplied = wbApplied;\n"
        "    }\n"
        "    if (previewApplyAgx > 0.5)\n"
        "    {\n"
        "        vec3 v = max(matrixApplied * 65535.0, 0.0);\n"
        "        vec3 a = vec3(dot(previewAgxFwd0, v), dot(previewAgxFwd1, v), dot(previewAgxFwd2, v));\n"
        "        matrixApplied = floor(clamp(a, 0.0, 65535.0)) / 65535.0;\n"
        "    }\n"
        "    matrixApplied = clamp(matrixApplied, 0.0, 1.0);\n"
        "    vec3 result = vec3(sampleU16Lut(gammaLut, matrixApplied.r), sampleU16Lut(gammaLut, matrixApplied.g), sampleU16Lut(gammaLut, matrixApplied.b));\n"
        "    if (previewApplyGradient > 0.5)\n"
        "    {\n"
        "        vec2 gfc = vec2(vTexCoord.x, 1.0 - vTexCoord.y) * frameSize;\n"
        "        float gblend = texture2D(gradientMask, (floor(gfc) + vec2(0.5)) / frameSize).r / 65535.0;\n"
        "        vec3 g = vec3(sampleU16Lut(gradMatrixLutR, leveled.r), sampleU16Lut(gradMatrixLutG, leveled.g), sampleU16Lut(gradMatrixLutB, leveled.b)) * 65535.0;\n"
        "        float gtmpGreen = g.g;\n"
        "        float vigF = 1.0;\n"
        "        if (previewApplyVignette > 0.5)\n"
        "        {\n"
        "            float vmidx = floor(gfc.y) * frameSize.x + floor(gfc.x) + 1.0;\n"
        "            if (vmidx < frameSize.x * frameSize.y)\n"
        "            {\n"
        "                float vmx = mod(vmidx, frameSize.x);\n"
        "                float vmy = floor(vmidx / frameSize.x);\n"
        "                float vm = texture2D(vignetteMask, (vec2(vmx, vmy) + vec2(0.5)) / frameSize).r;\n"
        "                float vb = 1.0 + (vm * previewVignetteStrength / 128.0);\n"
        "                float vb2 = vb * vb;\n"
        "                vigF = vb2 * vb2;\n"
        "            }\n"
        "        }\n"
        "        float baseContrastF = 1.0;\n"
        "        float gradContrastF = 1.0;\n"
        "        if (previewApplyInLoopContrast > 0.5 || previewApplyGradientContrast > 0.5)\n"
        "        {\n"
        "            vec3 bm = vec3(sampleU16Lut(matrixLutR, leveled.r), sampleU16Lut(matrixLutG, leveled.g), sampleU16Lut(matrixLutB, leveled.b)) * vigF;\n"
        "            vec3 bm16 = floor(bm * 65535.0 + 0.5);\n"
        "            float gcval = floor((bm16.r * 4.0 + bm16.g * 11.0 + bm16.b) / 16.0);\n"
        "            if (previewApplyInLoopContrast > 0.5) baseContrastF = sampleContrastCurve(inLoopContrastCurve, gcval);\n"
        "            if (previewApplyGradientContrast > 0.5) gradContrastF = sampleContrastCurve(gradientContrastCurve, gcval);\n"
        "        }\n"
        "        g = clamp(g * (vigF * shadowsHighlightsF * baseContrastF * gradContrastF), 0.0, 65535.0);\n"
        "        if (previewApplyHighlightRecon > 0.5)\n"
        "        {\n"
        "            float gt1 = floor(clamp(gtmpGreen + 0.5, 0.0, 65535.0));\n"
        "            bool grep = false;\n"
        "            if (previewHighlightReconDualIso > 0.5)\n"
        "            {\n"
        "                float glo = clamp(previewGradientHighestGreenDiso - 5000.0, 0.0, 65535.0);\n"
        "                float ghi = clamp(previewGradientHighestGreenDiso + 5000.0, 0.0, 65535.0);\n"
        "                if (gt1 >= glo && gt1 <= ghi && g.g < 1.1 * g.r && g.g < g.b) grep = true;\n"
        "            }\n"
        "            else if (gt1 == previewGradientHighestGreen)\n"
        "            {\n"
        "                grep = true;\n"
        "            }\n"
        "            if (grep) g.g = (g.r + g.b) / 2.0;\n"
        "        }\n"
        "        vec3 gmv = g / 65535.0;\n"
        "        if (previewUseCameraMatrix > 0.5)\n"
        "        {\n"
        "            vec3 gw = vec3(dot(previewProperWbRow0, gmv), dot(previewProperWbRow1, gmv), dot(previewProperWbRow2, gmv));\n"
        "            if (previewApplyGamutCompression > 0.5)\n"
        "            {\n"
        "                float Y = dot(previewRgbToY, gw);\n"
        "                float minC = min(min(gw.r, gw.g), gw.b);\n"
        "                vec3 gref = vec3(-(reinhardForColour((Y != 0.0) ? ((Y - gw.r) / Y) : 0.0) * Y) + Y,\n"
        "                                 -(reinhardForBlue((Y != 0.0) ? ((Y - gw.g) / Y) : 0.0) * Y) + Y,\n"
        "                                 -(reinhardForBlue((Y != 0.0) ? ((Y - gw.b) / Y) : 0.0) * Y) + Y);\n"
        "                float gmin = min(min(gref.r, gref.g), gref.b);\n"
        "                float gdes = 1.0;\n"
        "                float gden = Y - minC;\n"
        "                if (Y > 0.0 && abs(gden) > 0.00000001) gdes = (Y - gmin) / gden;\n"
        "                gw = (gw - vec3(Y)) * gdes + vec3(Y);\n"
        "            }\n"
        "            gmv = gw;\n"
        "        }\n"
        "        if (previewApplyAgx > 0.5)\n"
        "        {\n"
        "            vec3 av = max(gmv * 65535.0, 0.0);\n"
        "            vec3 aa = vec3(dot(previewAgxFwd0, av), dot(previewAgxFwd1, av), dot(previewAgxFwd2, av));\n"
        "            gmv = floor(clamp(aa, 0.0, 65535.0)) / 65535.0;\n"
        "        }\n"
        "        vec3 pixg = vec3(sampleU16LutTrunc(gradGammaLut, gmv.r), sampleU16LutTrunc(gradGammaLut, gmv.g), sampleU16LutTrunc(gradGammaLut, gmv.b));\n"
        "        vec3 gblended = vec3(gblend) * pixg + vec3(1.0 - gblend) * result;\n"
        "        result = floor(clamp(gblended * 65535.0, 0.0, 65535.0)) / 65535.0;\n"
        "    }\n"
        "    if (previewApplyHueVs > 0.5)\n"
        "    {\n"
        "        vec3 hv = result * 65535.0;\n"
        "        vec3 hsl = previewFromRGBtoHSV(result);\n"
        "        float hsat = 0.0;\n"
        "        if (!(hv.r < 0.5 && hv.g < 0.5 && hv.b < 0.5))\n"
        "        {\n"
        "            float hbig = max(max(hv.r, hv.g), hv.b);\n"
        "            float hsmall = min(min(hv.r, hv.g), hv.b);\n"
        "            hsat = (hbig > 0.0) ? (hbig - hsmall) / hbig : 0.0;\n"
        "        }\n"
        "        hsat = 2.0 * hsat / (hsat * hsat + 1.0);\n"
        "        hsat = min(hsat, 1.0);\n"
        "        float hueIndex = floor(hsl.x * 100.0);\n"
        "        hsl.z *= 1.0 + (sampleHueVsCurve(hueVsLumaCurve, hueIndex) * hsat * 2.0);\n"
        "        hsl.z = max(hsl.z, 0.0);\n"
        "        hsl.y *= 1.0 + (sampleHueVsCurve(hueVsSaturationCurve, hueIndex) * 2.0);\n"
        "        hsl.y = max(hsl.y, 0.0);\n"
        "        hsl.x += 60.0 * sampleHueVsCurve(hueVsHueCurve, hueIndex);\n"
        "        if (hsl.x < 0.0) hsl.x += 360.0;\n"
        "        else if (hsl.x >= 360.0) hsl.x -= 360.0;\n"
        "        float lumaIndex = floor(hsl.z * 36000.0);\n"
        "        hsl.y *= 1.0 + (sampleHueVsCurve(lumaVsSaturationCurve, lumaIndex) * 2.0);\n"
        "        hsl.y = max(hsl.y, 0.0);\n"
        "        vec3 hrgb = previewFromHSVtoRGB(hsl);\n"
        "        result = clamp(floor(hrgb * 65535.0 + 0.5), 0.0, 65535.0) / 65535.0;\n"
        "    }\n"
        "    if (previewApplyVibrance > 0.5)\n"
        "    {\n"
        "        vec3 vv = result * 65535.0;\n"
        "        float vibY = floor((vv.r * 4.0 + vv.g * 11.0 + vv.b) / 16.0);\n"
        "        vec3 vpix0 = truncToZero((vv - vec3(vibY)) * previewVibrance) + vec3(vibY);\n"
        "        if (previewVibrance > 1.0)\n"
        "        {\n"
        "            float vbig = max(max(vv.r, vv.g), vv.b);\n"
        "            float vsmall = min(min(vv.r, vv.g), vv.b);\n"
        "            float vs = (vbig > 0.0) ? (vbig - vsmall) / vbig : 0.0;\n"
        "            vs = 2.0 * vs / (vs * vs + 1.0);\n"
        "            vs = min(vs, 1.0);\n"
        "            result = clamp(vv * vs + vpix0 * (1.0 - vs), 0.0, 65535.0) / 65535.0;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            result = clamp(vpix0, 0.0, 65535.0) / 65535.0;\n"
        "        }\n"
        "    }\n"
        "    if (previewApplySaturation > 0.5)\n"
        "    {\n"
        "        vec3 sv = result * 65535.0;\n"
        "        float satY = floor((sv.r * 4.0 + sv.g * 11.0 + sv.b) / 16.0);\n"
        "        result = clamp(truncToZero((sv - vec3(satY)) * previewSaturation) + vec3(satY), 0.0, 65535.0) / 65535.0;\n"
        "    }\n"
        "    if (previewApplyToning > 0.5)\n"
        "    {\n"
        "        result = clamp(truncToZero(result * 65535.0 * previewToningGain), 0.0, 65535.0) / 65535.0;\n"
        "    }\n"
        "    if (previewApplyCreativeCurves > 0.5)\n"
        "    {\n"
        "        result = vec3(sampleU16Lut(contrastCurveLut, result.r), sampleU16Lut(contrastCurveLut, result.g), sampleU16Lut(contrastCurveLut, result.b));\n"
        "        result = vec3(sampleU16Lut(gradationLutY, result.r), sampleU16Lut(gradationLutY, result.g), sampleU16Lut(gradationLutY, result.b));\n"
        "        result = vec3(sampleU16Lut(gradationLutR, result.r), sampleU16Lut(gradationLutG, result.g), sampleU16Lut(gradationLutB, result.b));\n"
        "    }\n"
        "    if (previewApplyAgx > 0.5)\n"
        "    {\n"
        "        vec3 f = result * 65535.0;\n"
        "        vec3 iv = vec3(dot(previewAgxInv0, f), dot(previewAgxInv1, f), dot(previewAgxInv2, f));\n"
        "        result = floor(clamp(iv, 0.0, 65535.0)) / 65535.0;\n"
        "    }\n"
        "    if (previewApplyLut > 0.5)\n"
        "    {\n"
        "        float dimF = previewLutDimension;\n"
        "        vec3 pixv = floor(result * 65535.0 + 0.5);\n"
        "        vec3 fABC = vec3(dimF - 1.0) / 65536.0 / (previewLutDomainMax - previewLutDomainMin);\n"
        "        vec3 sc = pixv * fABC - previewLutDomainMin;\n"
        "        vec3 base = max(floor(sc), vec3(0.0));\n"
        "        vec3 i0 = min(base, vec3(dimF - 1.0));\n"
        "        vec3 i1 = min(base + 1.0, vec3(dimF - 1.0));\n"
        "        vec3 outc;\n"
        "        if (previewLut3d > 0.5)\n"
        "        {\n"
        "            vec3 fr = sc - i0;\n"
        "            float rf = fr.r; float gf = fr.g; float bf = fr.b;\n"
        "            vec3 q000 = texture3D(lut3dTexture, (vec3(i0.r, i0.g, i0.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q001 = texture3D(lut3dTexture, (vec3(i0.r, i0.g, i1.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q010 = texture3D(lut3dTexture, (vec3(i0.r, i1.g, i0.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q011 = texture3D(lut3dTexture, (vec3(i0.r, i1.g, i1.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q100 = texture3D(lut3dTexture, (vec3(i1.r, i0.g, i0.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q101 = texture3D(lut3dTexture, (vec3(i1.r, i0.g, i1.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q110 = texture3D(lut3dTexture, (vec3(i1.r, i1.g, i0.b) + 0.5) / dimF).rgb;\n"
        "            vec3 q111 = texture3D(lut3dTexture, (vec3(i1.r, i1.g, i1.b) + 0.5) / dimF).rgb;\n"
        "            if (gf >= bf && bf >= rf) outc = (1.0 - gf) * q000 + (gf - bf) * q010 + (bf - rf) * q011 + rf * q111;\n"
        "            else if (bf > rf && rf > gf) outc = (1.0 - bf) * q000 + (bf - rf) * q001 + (rf - gf) * q101 + gf * q111;\n"
        "            else if (bf > gf && gf >= rf) outc = (1.0 - bf) * q000 + (bf - gf) * q001 + (gf - rf) * q011 + rf * q111;\n"
        "            else if (rf >= gf && gf > bf) outc = (1.0 - rf) * q000 + (rf - gf) * q100 + (gf - bf) * q110 + bf * q111;\n"
        "            else if (gf > rf && rf >= bf) outc = (1.0 - gf) * q000 + (gf - rf) * q010 + (rf - bf) * q110 + bf * q111;\n"
        "            else outc = (1.0 - rf) * q000 + (rf - bf) * q100 + (bf - gf) * q101 + gf * q111;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            float r0v = texture2D(lut1dTexture, vec2((i0.r + 0.5) / dimF, 0.5)).r;\n"
        "            float r1v = texture2D(lut1dTexture, vec2((i1.r + 0.5) / dimF, 0.5)).r;\n"
        "            float g0v = texture2D(lut1dTexture, vec2((i0.g + 0.5) / dimF, 0.5)).g;\n"
        "            float g1v = texture2D(lut1dTexture, vec2((i1.g + 0.5) / dimF, 0.5)).g;\n"
        "            float b0v = texture2D(lut1dTexture, vec2((i0.b + 0.5) / dimF, 0.5)).b;\n"
        "            float b1v = texture2D(lut1dTexture, vec2((i1.b + 0.5) / dimF, 0.5)).b;\n"
        "            outc = vec3(lutLerp1d(sc.r, i0.r, i1.r, r0v, r1v), lutLerp1d(sc.g, i0.g, i1.g, g0v, g1v), lutLerp1d(sc.b, i0.b, i1.b, b0v, b1v));\n"
        "        }\n"
        "        vec3 limited = clamp(outc * 65535.0, 0.0, 65535.0);\n"
        "        vec3 blended = pixv * (1.0 - previewLutIntensity) + limited * previewLutIntensity;\n"
        "        result = floor(clamp(blended, 0.0, 65535.0)) / 65535.0;\n"
        "    }\n"
        "    return result;\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec2 sourceCoord = vec2(vTexCoord.x, 1.0 - vTexCoord.y);\n"
        "    vec4 sampledColor = texture2D(frameTexture, sourceCoord);\n"
        "    gl_FragColor = vec4(applyPreviewProcessing(sampledColor.rgb), sampledColor.a);\n"
        "}\n");
}

QByteArray gpuPreviewProcessingPackLookupTextureRgba16(const QByteArray & sourceLut)
{
    QByteArray packed(kLutTextureEdge * kLutTextureEdge * 4 * static_cast<int>(sizeof(uint16_t)),
                      Qt::Uninitialized);
    std::memset(packed.data(), 0, static_cast<size_t>(packed.size()));
    if ( sourceLut.size() < static_cast<int>(65536u * sizeof(uint16_t)) )
    {
        return packed;
    }

    const uint16_t * sourceValues = reinterpret_cast<const uint16_t *>(sourceLut.constData());
    uint16_t * destValues = reinterpret_cast<uint16_t *>(packed.data());
    for (int index = 0; index < 65536; ++index)
    {
        const uint16_t value = sourceValues[index];
        destValues[index * 4 + 0] = value;
        destValues[index * 4 + 1] = value;
        destValues[index * 4 + 2] = value;
        destValues[index * 4 + 3] = 65535;
    }
    return packed;
}

bool gpuPreviewProcessingEnsureDisplayProgram(QOpenGLShaderProgram *& program,
                                              QObject * shaderParent)
{
    if ( program ) return true;

    const QByteArray vertexShader = gpuPreviewProcessingVertexShaderSource();
    const QByteArray fragmentShader = gpuPreviewProcessingDisplayFragmentShaderSource();

    program = new QOpenGLShaderProgram(shaderParent);
    if ( !program->addShaderFromSourceCode(QOpenGLShader::Vertex, vertexShader)
      || !program->addShaderFromSourceCode(QOpenGLShader::Fragment, fragmentShader)
      || !program->link() )
    {
        qWarning() << "GPU preview-processing display shader setup failed:" << program->log();
        delete program;
        program = nullptr;
        return false;
    }
    return true;
}

QOpenGLTexture * gpuPreviewProcessingCreateOrResizeLookupTexture(QOpenGLTexture * texture,
                                                                 int width,
                                                                 int height)
{
    if ( texture && texture->isCreated() && texture->width() == width && texture->height() == height )
    {
        return texture;
    }

    delete texture;
    texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
    texture->setSize(width, height);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 2): allocateStorage() is void, so
    // isCreated() is the only signal that the GL texture object actually came into
    // being (e.g. it stays false with no current context or a lost context). A caller
    // that got a non-null pointer back here used to be indistinguishable from one that
    // got a real GPU texture; now a failed allocation returns nullptr instead.
    if ( !texture->isCreated() )
    {
        delete texture;
        return nullptr;
    }
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

void gpuPreviewProcessingDestroyLutTextureSet(GpuPreviewProcessingLutTextureSet & set)
{
    delete set.levels;
    delete set.matrixR;
    delete set.matrixG;
    delete set.matrixB;
    delete set.gamma;
    set.levels = nullptr;
    set.matrixR = nullptr;
    set.matrixG = nullptr;
    set.matrixB = nullptr;
    set.gamma = nullptr;
    set.signature = 0;
    set.signatureValid = false;
}

void gpuPreviewProcessingUpdateLutTextureSet(GpuPreviewProcessingLutTextureSet & set,
                                             const GpuPreviewProcessingConfig & config)
{
    if ( !config.enabled )
    {
        gpuPreviewProcessingDestroyLutTextureSet(set);
        return;
    }
    if ( set.signatureValid
      && set.signature == config.signature
      && set.levels && set.matrixR && set.matrixG && set.matrixB && set.gamma )
    {
        return;
    }
    if ( config.levelsLut.size() < static_cast<int>(65536u * sizeof(uint16_t))
      || config.matrixLutR.size() < static_cast<int>(65536u * sizeof(uint16_t))
      || config.matrixLutG.size() < static_cast<int>(65536u * sizeof(uint16_t))
      || config.matrixLutB.size() < static_cast<int>(65536u * sizeof(uint16_t))
      || config.gammaLut.size() < static_cast<int>(65536u * sizeof(uint16_t)) )
    {
        gpuPreviewProcessingDestroyLutTextureSet(set);
        return;
    }

    const QByteArray levelsBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.levelsLut);
    const QByteArray matrixRBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutR);
    const QByteArray matrixGBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutG);
    const QByteArray matrixBBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutB);
    const QByteArray gammaBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.gammaLut);

    set.levels = gpuPreviewProcessingCreateOrResizeLookupTexture(set.levels, kLutTextureEdge, kLutTextureEdge);
    set.matrixR = gpuPreviewProcessingCreateOrResizeLookupTexture(set.matrixR, kLutTextureEdge, kLutTextureEdge);
    set.matrixG = gpuPreviewProcessingCreateOrResizeLookupTexture(set.matrixG, kLutTextureEdge, kLutTextureEdge);
    set.matrixB = gpuPreviewProcessingCreateOrResizeLookupTexture(set.matrixB, kLutTextureEdge, kLutTextureEdge);
    set.gamma = gpuPreviewProcessingCreateOrResizeLookupTexture(set.gamma, kLutTextureEdge, kLutTextureEdge);

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 2): a real GL allocation failure
    // (lost/recreated context, out of memory) now surfaces as a null member above
    // instead of a stale/half-built set silently being stamped ready. Destroy
    // whatever did get created and leave signatureValid false so the next call
    // retries from scratch rather than presenting with a subset of LUTs bound.
    if ( !set.levels || !set.matrixR || !set.matrixG || !set.matrixB || !set.gamma )
    {
        gpuPreviewProcessingDestroyLutTextureSet(set);
        return;
    }

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 3): setData() is void, so a failed
    // upload (e.g. a driver rejecting the transfer on an otherwise-created texture) was
    // previously indistinguishable from a successful one -- signatureValid was stamped
    // regardless. Drain any pre-existing GL error first so a stale error from unrelated
    // prior work is never misattributed to these uploads, then check once after all five
    // -- any error found leaves signatureValid false (destroying the whole set, same as
    // an allocation failure above) rather than presenting with corrupt LUT content.
    QOpenGLContext * currentContext = QOpenGLContext::currentContext();
    QOpenGLFunctions * gl = currentContext ? currentContext->functions() : nullptr;
    if ( gl )
    {
        // BOUNDED (GPU-TEXNR-S1-DARK-GREEN-1 round 4, fable minor): on a robustness-
        // enabled context (GL_KHR_robustness), glGetError() can report GL_CONTEXT_LOST
        // (0x0507) persistently until the app handles the reset, which would otherwise
        // spin this drain forever. Cap it at 16 iterations -- comfortably above any real
        // burst of stale errors -- and treat a lost context as not-ready immediately
        // rather than draining through it and attempting five uploads that cannot
        // succeed.
        constexpr GLenum kGlContextLost = 0x0507;
        int drainIterations = 0;
        GLenum drainedError = GL_NO_ERROR;
        while ( drainIterations < 16 && (drainedError = gl->glGetError()) != GL_NO_ERROR )
        {
            if ( drainedError == kGlContextLost )
            {
                gpuPreviewProcessingDestroyLutTextureSet(set);
                return;
            }
            ++drainIterations;
        }
    }

    set.levels->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, levelsBytes.constData());
    set.matrixR->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, matrixRBytes.constData());
    set.matrixG->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, matrixGBytes.constData());
    set.matrixB->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, matrixBBytes.constData());
    set.gamma->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gammaBytes.constData());

    const GLenum uploadError = gl ? gl->glGetError() : GL_NO_ERROR;
    if ( !gl || uploadError != GL_NO_ERROR )
    {
        gpuPreviewProcessingDestroyLutTextureSet(set);
        return;
    }

    set.signature = config.signature;
    set.signatureValid = true;
}

bool gpuPreviewProcessingLutTextureSetReady(const GpuPreviewProcessingLutTextureSet & set,
                                            const GpuPreviewProcessingConfig & config)
{
    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 2): signatureValid is only set by
    // gpuPreviewProcessingUpdateLutTextureSet() after every LUT texture is confirmed
    // GL-created and uploaded (see above) -- checking it here, not just the pointers,
    // is what makes readiness reflect actual GL success rather than "five non-null
    // C++ wrappers", which a never-created QOpenGLTexture would otherwise satisfy.
    return config.enabled
        && set.signatureValid
        && set.levels && set.matrixR && set.matrixG && set.matrixB && set.gamma;
}

bool gpuPreviewProcessingReconTexturePresentationRefused(
    bool presentingReconTexture,
    const GpuPreviewProcessingLutTextureSet & set,
    const GpuPreviewProcessingConfig & config)
{
    return presentingReconTexture && !gpuPreviewProcessingLutTextureSetReady(set, config);
}

void gpuPreviewProcessingBindDisplayUniformsAndTextures(
    QOpenGLShaderProgram * program,
    const GpuPreviewProcessingConfig & config,
    const GpuPreviewProcessingLutTextureSet & lutSet,
    const GpuPreviewProcessingDisplayUniforms & uniforms,
    bool lutsReady)
{
    if ( !program ) return;

    program->setUniformValue("textureSize", uniforms.textureSize);
    program->setUniformValue("frameTextureMode", uniforms.frameTextureMode);
    program->setUniformValue("samplingMode", uniforms.samplingMode);
    program->setUniformValue("zebraEnabled", uniforms.zebraEnabled ? 1.0f : 0.0f);
    program->setUniformValue("zebraUnderThreshold", uniforms.zebraUnderThreshold);
    program->setUniformValue("zebraOverThreshold", uniforms.zebraOverThreshold);
    program->setUniformValue("previewProcessingEnabled", lutsReady ? 1.0f : 0.0f);
    program->setUniformValue("previewUseCameraMatrix", config.useCameraMatrix ? 1.0f : 0.0f);
    program->setUniformValue("previewApplyGamutCompression", config.applyGamutCompression ? 1.0f : 0.0f);
    program->setUniformValue("previewProperWbRow0",
                             QVector3D(config.properWbMatrix[0], config.properWbMatrix[1], config.properWbMatrix[2]));
    program->setUniformValue("previewProperWbRow1",
                             QVector3D(config.properWbMatrix[3], config.properWbMatrix[4], config.properWbMatrix[5]));
    program->setUniformValue("previewProperWbRow2",
                             QVector3D(config.properWbMatrix[6], config.properWbMatrix[7], config.properWbMatrix[8]));
    program->setUniformValue("previewRgbToY",
                             QVector3D(config.rgbToY[0], config.rgbToY[1], config.rgbToY[2]));

    if ( lutsReady && lutSet.levels )
    {
        program->setUniformValue("levelsLut", 1);
        lutSet.levels->bind(1);
    }
    if ( lutsReady && lutSet.matrixR )
    {
        program->setUniformValue("matrixLutR", 2);
        lutSet.matrixR->bind(2);
    }
    if ( lutsReady && lutSet.matrixG )
    {
        program->setUniformValue("matrixLutG", 3);
        lutSet.matrixG->bind(3);
    }
    if ( lutsReady && lutSet.matrixB )
    {
        program->setUniformValue("matrixLutB", 4);
        lutSet.matrixB->bind(4);
    }
    if ( lutsReady && lutSet.gamma )
    {
        program->setUniformValue("gammaLut", 5);
        lutSet.gamma->bind(5);
    }
}

void gpuPreviewProcessingReleaseDisplayTextures(const GpuPreviewProcessingLutTextureSet & lutSet,
                                                bool lutsReady)
{
    if ( lutsReady && lutSet.levels ) lutSet.levels->release();
    if ( lutsReady && lutSet.matrixR ) lutSet.matrixR->release();
    if ( lutsReady && lutSet.matrixG ) lutSet.matrixG->release();
    if ( lutsReady && lutSet.matrixB ) lutSet.matrixB->release();
    if ( lutsReady && lutSet.gamma ) lutSet.gamma->release();
}

bool gpuPreviewProcessingRendererIsSoftware(const QString & rendererDescription)
{
    const QString normalized = rendererDescription.trimmed().toLower();
    return normalized.contains(QStringLiteral("llvmpipe"))
        || normalized.contains(QStringLiteral("softpipe"))
        || normalized.contains(QStringLiteral("software rasterizer"))
        || normalized.contains(QStringLiteral("software renderer"))
        || normalized.contains(QStringLiteral("swiftshader"))
        || normalized.contains(QStringLiteral("warp"))
        || normalized.contains(QStringLiteral("microsoft basic render"))
        || normalized.contains(QStringLiteral("gdi generic"));
}

bool gpuPreviewProcessingIsSupported(const processingObject_t * processing,
                                     QString * reason)
{
    auto reject = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( !processing ) return reject(QStringLiteral("processing object missing"));
    /* highlight_reconstruction is now supported: a per-pixel clipped-green replace
     * (green -> (R+B)/2) keyed on the matrix-green white level / dual-ISO peak,
     * ported as a per-pixel stage (no spatial pass). No reject. */
    /* allow_creative_adjustments no longer has its own reject: the GPU subset
     * shader ports every creative-family stage -- the in-loop simple-contrast
     * factor, hue-vs / luma-vs curves, vibrance, saturation, toning, the contrast
     * curve (pre_calc_curve_r) and the gradation curves (gcurve_*). Clips are
     * still failed closed below on the non-creative features the shader does not
     * implement (gradient, LUT, filter, AgX, denoise, grain, CA, sharpen, chroma
     * separation/blur, clarity, vignette, non-Rec709 gamut),
     * which are gated independently of the creative flag. */
    /* gradient is now supported: a second pre-creative pipeline through the
     * gradient LUTs, blended into the base in gamma space by the gradient mask
     * before the creative chain. Ported as a per-pixel stage (no spatial pass).
     * Reject only when enabled-but-unbuilt (no mask), which the subset cannot
     * reproduce. */
    if ( processing->gradient_enable && processing->gradient_mask == NULL )
    {
        return reject(QStringLiteral("gradient mask unavailable"));
    }
    /* 1D/3D .cube LUTs are supported: applied as the last stage (tetrahedral 3D /
     * per-channel-lerp 1D) from a volume/1D texture. Reject only a malformed cube
     * (the subset cannot reproduce a LUT without valid cube data). */
    if ( processing->lut_on
      && (!processing->lut || !processing->lut->cube || processing->lut->dimension <= 1) )
    {
        return reject(QStringLiteral("LUT enabled but cube unavailable"));
    }
    if ( processing->filter_on ) return reject(QStringLiteral("filter enabled"));
    /* AgX is now supported: a forward compressed-gamut matmul before gamma and
     * the inverse after the creative curves, carried as uniform matrices (no new
     * textures). No reject. */
    /* EXR/cyan-highlight mode is compatible with the preview subset as long as
     * gamut compression stays disabled. The config builder already derives that
     * via applyGamutCompression = false, so do not reject it here. */
    /* Median denoise is supported for windows up to 5 (the GLSL window-median is
     * O(winSize^2)); a larger window is rejected. */
    if ( processing->denoiserStrength > 0 && processing->denoiserWindow > 5 )
    {
        return reject(QStringLiteral("median denoiser window exceeds 5"));
    }
    if ( processing->rbfDenoiserLuma > 0 || processing->rbfDenoiserChroma > 0 ) return reject(QStringLiteral("RBF denoiser enabled"));
    if ( processing->grainStrength > 0 ) return reject(QStringLiteral("grain enabled"));
    if ( processing->ca_desaturate > 0 ) return reject(QStringLiteral("CA correction enabled"));
    /* Sharpen is supported standalone (a 5-tap cross post-pass). The engine
     * interleaves sharpen-on-Y inside the chroma YCbCr round-trip and offers an
     * optional sobel edge mask; those combined paths are not yet ported, so reject
     * sharpen + chroma-separation and sharpen + sobel-mask. */
    if ( processing->sharpen > 0.005 && processing->sh_masking > 0 )
    {
        return reject(QStringLiteral("sharpen edge mask enabled"));
    }
    if ( processing->sharpen > 0.005 && processing->cs_zone.use_cs )
    {
        return reject(QStringLiteral("sharpen with chroma separation enabled"));
    }
    /* Chroma separation/blur is supported: a YCbCr round-trip + optional box blur
     * of Cb/Cr as a post-pass. Reject only an over-radius chroma blur (the GPU box
     * blur is float32-exact only up to radius 127); chroma_blur_radius without
     * use_cs is a no-op in the engine, so it needs no reject. */
    if ( processing->cs_zone.use_cs && processing->cs_zone.chroma_blur_radius > 127 )
    {
        return reject(QStringLiteral("chroma blur radius exceeds 127"));
    }
    if ( std::fabs(processing->clarity) >= 0.01 ) return reject(QStringLiteral("clarity enabled"));
    /* Shadows/highlights are supported when the caller attaches the current
     * frame's blur mask after the production processing pre-pass has refreshed
     * it. No reject here; gpuPreviewProcessingApplyGpuOffscreen fails closed if
     * frame state is missing. */
    /* Vignette is supported via a full-frame R32F mask texture sampled by the
     * fragment's raster index (the vmpix off-by-one + vignette_end guard mirrored).
     * Reject only when the strength is set but the mask buffer was never built
     * (the subset cannot reproduce vignette without the mask). */
    if ( processing->vignette_strength != 0 && processing->vignette_mask == NULL )
    {
        return reject(QStringLiteral("vignette mask unavailable"));
    }
    /* Non-Rec709 gamut is now supported: the gamut is baked into proper_wb_matrix
     * (already applied in-shader) and the gamut-compression luma weights are
     * derived per-gamut in gpuPreviewProcessingBuildConfig (rgbToY). No reject. */

    if ( reason ) reason->clear();
    return true;
}

GpuPreviewProcessingConfig gpuPreviewProcessingBuildConfig(
    const processingObject_t * processing,
    QString * reason)
{
    GpuPreviewProcessingConfig config;
    if ( !gpuPreviewProcessingIsSupported(processing, reason) )
    {
        return config;
    }

    config.enabled = true;
    config.useCameraMatrix = processing->use_cam_matrix > 0;
    config.applyGamutCompression = config.useCameraMatrix && !processing->exr_mode;
    /* Exposure for the supported subset is preserved through the copied LUTs:
     * negative exposure is already folded into pre_calc_matrix, while positive
     * exposure is already folded into pre_calc_gamma. Keep the source value in
     * the config so tests and future readers can verify that relationship. */
    config.sourceExposureStops = static_cast<float>(processing->exposure_stops);
    for (int index = 0; index < 9; ++index)
    {
        config.properWbMatrix[index] = static_cast<float>(processing->proper_wb_matrix[index]);
    }
    /* Gamut-compression desaturation weights. raw_processing derives rgb_to_Y
     * per gamut (second row of inverse(gamut matrix), raw_processing.c ~2680).
     * Rec709 keeps the exact hardcoded constants so the validated golden output
     * is byte-stable; other gamuts use the engine-derived weights so the GPU
     * gamut-compression path matches production. The matrix itself already
     * carries the gamut via proper_wb_matrix. */
    if ( processing->colour_gamut == GAMUT_Rec709 )
    {
        std::memcpy(config.rgbToY, kRec709RgbToY, sizeof(config.rgbToY));
    }
    else
    {
        double gamutRgbToY[3] = { 0.0, 0.0, 0.0 };
        processingGamutRgbToY(processing->colour_gamut, gamutRgbToY);
        for (int index = 0; index < 3; ++index)
        {
            config.rgbToY[index] = static_cast<float>(gamutRgbToY[index]);
        }
    }
    config.levelsLut = QByteArray(
        reinterpret_cast<const char *>(processing->pre_calc_levels),
        static_cast<int>(65536u * sizeof(uint16_t)));
    config.matrixLutR.resize(static_cast<int>(65536u * sizeof(uint16_t)));
    config.matrixLutG.resize(static_cast<int>(65536u * sizeof(uint16_t)));
    config.matrixLutB.resize(static_cast<int>(65536u * sizeof(uint16_t)));
    uint16_t * matrixR = reinterpret_cast<uint16_t *>(config.matrixLutR.data());
    uint16_t * matrixG = reinterpret_cast<uint16_t *>(config.matrixLutG.data());
    uint16_t * matrixB = reinterpret_cast<uint16_t *>(config.matrixLutB.data());
    for (int index = 0; index < 65536; ++index)
    {
        matrixR[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix[0][index], 65535));
        matrixG[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix[4][index], 65535));
        matrixB[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix[8][index], 65535));
    }
    config.gammaLut = QByteArray(
        reinterpret_cast<const char *>(processing->pre_calc_gamma),
        static_cast<int>(65536u * sizeof(uint16_t)));

    /* Slice 1: post-gamma creative curves (contrast + gradation) ported to the
     * GPU subset shader. gpuPreviewProcessingIsSupported only accepts
     * allow_creative_adjustments when the not-yet-ported creative stages are
     * neutral, so these prebuilt uint16[65536] LUTs are safe to apply verbatim.
     * Order matches raw_processing.c:3696-3738. */
    config.applyCreativeCurves = processing->allow_creative_adjustments != 0;
    config.applyToning = processing->allow_creative_adjustments != 0
                      && (processing->toning_dry < 0.998f
                          || std::fabs(processing->toning_wet[0]) > 0.0005f
                          || std::fabs(processing->toning_wet[1]) > 0.0005f
                          || std::fabs(processing->toning_wet[2]) > 0.0005f);
    config.toningGain[0] = static_cast<float>(processing->toning_dry + processing->toning_wet[0]);
    config.toningGain[1] = static_cast<float>(processing->toning_dry + processing->toning_wet[1]);
    config.toningGain[2] = static_cast<float>(processing->toning_dry + processing->toning_wet[2]);
    config.applyVibrance = processing->allow_creative_adjustments != 0
                        && (processing->vibrance > 1.01 || processing->vibrance < 0.99);
    config.vibrance = static_cast<float>(processing->vibrance);
    config.applySaturation = processing->allow_creative_adjustments != 0
                          && (processing->saturation > 1.01 || processing->saturation < 0.99);
    config.saturation = static_cast<float>(processing->saturation);
    config.applyHueVs = processing->allow_creative_adjustments != 0
                     && (processing->hue_vs_hue_used
                      || processing->hue_vs_saturation_used
                      || processing->hue_vs_luma_used
                      || processing->luma_vs_saturation_used);
    if ( config.applyHueVs )
    {
        /* hue-vs / luma-vs curves are float[36000] in [-1,1]; copied verbatim so
         * the CPU reference and the R32F GPU textures share the exact values
         * (raw_processing.c:3523-3578). */
        const int curveBytes = static_cast<int>(36000u * sizeof(float));
        config.hueVsHueCurve = QByteArray(
            reinterpret_cast<const char *>(processing->hue_vs_hue), curveBytes);
        config.hueVsSaturationCurve = QByteArray(
            reinterpret_cast<const char *>(processing->hue_vs_saturation), curveBytes);
        config.hueVsLumaCurve = QByteArray(
            reinterpret_cast<const char *>(processing->hue_vs_luma), curveBytes);
        config.lumaVsSaturationCurve = QByteArray(
            reinterpret_cast<const char *>(processing->luma_vs_saturation), curveBytes);
    }
    config.applyInLoopContrast = processing->allow_creative_adjustments != 0
                              && std::fabs(processing->contrast) >= 0.01;
    config.sourceContrast = static_cast<float>(processing->contrast);
    if ( config.applyInLoopContrast )
    {
        /* contrast_curve is double[65536] per-luma exposure factors; narrow to
         * float[65536] so the GPU R32F texture and the CPU reference share the
         * exact same values (raw_processing.c:2954). */
        config.inLoopContrastCurve.resize(static_cast<int>(65536u * sizeof(float)));
        float * dst = reinterpret_cast<float *>(config.inLoopContrastCurve.data());
        for (int index = 0; index < 65536; ++index)
        {
            dst[index] = static_cast<float>(processing->contrast_curve[index]);
        }
    }
    config.applyAgx = processing->AgX != 0;
    if ( config.applyAgx )
    {
        /* AgX is a forward compressed-gamut matmul before gamma + the inverse
         * after the creative curves (raw_processing_8bit_kernel.inc:264-281 /
         * 358-368). Carry float-narrowed copies of the engine's double matrices;
         * +/-1 LSB vs the production double path, within the parity tolerance. */
        double fwd[9] = { 0.0 };
        double inv[9] = { 0.0 };
        processingAgxMatrices(fwd, inv);
        for (int index = 0; index < 9; ++index)
        {
            config.agxForward[index] = static_cast<float>(fwd[index]);
            config.agxInverse[index] = static_cast<float>(inv[index]);
        }
    }
    config.applyVignette = processing->vignette_strength != 0;
    config.vignetteStrength = processing->vignette_strength;
    if ( config.applyVignette )
    {
        /* The vignette mask is a full-resolution float[w*h] alpha buffer
         * (processing_object.h:209), copied verbatim. Application matches
         * raw_processing.c:2881-2889: expo_correction *= pow(1 + mask*strength/128,
         * 4), with the vmpix pre-increment (pixel i reads mask[i+1]) and the
         * vignette_end guard (last pixel skips). The gate already failed closed if
         * the mask was unbuilt, so it is non-null here. */
        const float * mask = processing->vignette_mask;
        const ptrdiff_t count = (mask && processing->vignette_end > mask)
            ? (processing->vignette_end - mask) : 0;
        if ( count > 0 )
        {
            config.vignetteMask = QByteArray(reinterpret_cast<const char *>(mask),
                                             static_cast<int>(count * static_cast<ptrdiff_t>(sizeof(float))));
        }
        else
        {
            config.applyVignette = false;
        }
    }
    config.applyShadowsHighlights =
        processingHasShadowsHighlightsAdjustments(processing) != 0;
    if ( config.applyShadowsHighlights )
    {
        config.shadowsHighlightsCurve.resize(static_cast<int>(65536u * sizeof(float)));
        float * dst = reinterpret_cast<float *>(config.shadowsHighlightsCurve.data());
        for (int index = 0; index < 65536; ++index)
        {
            dst[index] = static_cast<float>(
                processing->shadows_highlights.shadow_highlight_curve[index]);
        }
        int curveIndexMask = 0;
        (void)processingGetShadowsHighlightsBlurData(
            processing, nullptr, nullptr, nullptr, &curveIndexMask);
        config.shadowsHighlightsCurveIndexMask = curveIndexMask != 0;
    }
    config.applyLut = processing->lut_on != 0 && processing->lut != NULL
                   && processing->lut->cube != NULL && processing->lut->dimension > 1;
    if ( config.applyLut )
    {
        /* LUT (.cube) is the engine's OUTPUT-stage stage (apply_lut, cube_lut.c:209-340,
         * run last at raw_processing.c:3767, not in the creative loop or direct8 kernel).
         * Copy the raw float cube + params; the shader + CPU reference replicate the
         * domain scaling, tetrahedral (3D) / per-channel-lerp (1D) interpolation, and the
         * intensity blend, applied as the last stage. */
        const lut_t * lut = processing->lut;
        config.lut3d = lut->is3d != 0;
        config.lutDimension = lut->dimension;
        const int clampedIntensity = lut->intensity > 100 ? 100 : lut->intensity;
        config.lutIntensity = clampedIntensity / 100.0f;
        for (int i = 0; i < 3; ++i)
        {
            config.lutDomainMin[i] = lut->domain_min[i];
            config.lutDomainMax[i] = lut->domain_max[i];
        }
        const int dim = config.lutDimension;
        const int entries = config.lut3d ? (dim * dim * dim) : dim;
        config.lutCube = QByteArray(reinterpret_cast<const char *>(lut->cube),
                                    entries * 3 * static_cast<int>(sizeof(float)));
    }
    /* Highlight reconstruction: a per-pixel clipped-green replace. highest_green
     * (non-dual-ISO white-level green) is computed once at matrix build
     * (processing_update_highest_green, processing.c:572); highest_green_diso is
     * the per-frame dual-ISO peak from analyse_frame_highest_green -- production
     * must refresh the config per frame so the diso peak is current. */
    config.applyHighlightReconstruction = processing->highlight_reconstruction != 0;
    config.highlightReconDualIso = (processing->dual_iso != NULL && *processing->dual_iso != 0);
    config.highestGreen = static_cast<int>(processing->highest_green);
    config.highestGreenDiso = static_cast<int>(processing->highest_green_diso);

    /* Gradient: a second pre-creative pipeline blended into the base by the
     * per-pixel mask. Active only when enabled with non-trivial gradient
     * exposure/contrast (the engine's use_gradient_adjustments) and a built mask.
     * The mask is carried as a pointer (frame-sized, no companion length here);
     * the callers read it by pixel index / width*height. */
    const bool gradientAdjustments =
        (processing->gradient_exposure_stops < -0.01 || processing->gradient_exposure_stops > 0.01)
     || (processing->gradient_contrast < -0.01 || processing->gradient_contrast > 0.01);
    config.applyGradient = processing->gradient_enable != 0
                        && gradientAdjustments
                        && processing->gradient_mask != NULL;
    if ( config.applyGradient )
    {
        config.gradientMaskData = processing->gradient_mask;
        config.applyGradientContrast = std::fabs(processing->gradient_contrast) >= 0.01;
        config.gradientHighestGreen = static_cast<int>(processing->highest_green_gradient);
        config.gradientHighestGreenDiso = static_cast<int>(processing->highest_green_gradient_diso);
        config.gradientMatrixLutR.resize(static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradientMatrixLutG.resize(static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradientMatrixLutB.resize(static_cast<int>(65536u * sizeof(uint16_t)));
        uint16_t * gmR = reinterpret_cast<uint16_t *>(config.gradientMatrixLutR.data());
        uint16_t * gmG = reinterpret_cast<uint16_t *>(config.gradientMatrixLutG.data());
        uint16_t * gmB = reinterpret_cast<uint16_t *>(config.gradientMatrixLutB.data());
        for (int index = 0; index < 65536; ++index)
        {
            gmR[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix_gradient[0][index], 65535));
            gmG[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix_gradient[4][index], 65535));
            gmB[index] = static_cast<uint16_t>(qBound(0, processing->pre_calc_matrix_gradient[8][index], 65535));
        }
        config.gradientGammaLut = QByteArray(
            reinterpret_cast<const char *>(processing->pre_calc_gamma_gradient),
            static_cast<int>(65536u * sizeof(uint16_t)));
        if ( config.applyGradientContrast )
        {
            config.gradientContrastCurve.resize(static_cast<int>(65536u * sizeof(float)));
            float * gdst = reinterpret_cast<float *>(config.gradientContrastCurve.data());
            for (int index = 0; index < 65536; ++index)
            {
                gdst[index] = static_cast<float>(processing->gradient_contrast_curve[index]);
            }
        }
    }
    /* Chroma separation/blur: a YCbCr round-trip post-pass with an optional box
     * blur of Cb/Cr (raw_processing.c:1816-1971). The engine runs the chroma blur
     * only inside the use_cs block, so chroma_blur_radius without use_cs is inert. */
    config.applyChroma = processing->cs_zone.use_cs != 0;
    config.chromaBlurRadius = config.applyChroma
        ? static_cast<int>(processing->cs_zone.chroma_blur_radius) : 0;

    /* Sharpen: a fixed 5-tap cross post-pass (processingSetSharpening,
     * raw_processing.c:4461). Supported standalone (the gate rejects it combined
     * with chroma separation or the sobel mask). Compute the engine's a/x/y from
     * the slider + bias in double so the GPU LUT and CPU reference match exactly. */
    config.applySharpen = processing->sharpen > 0.005
                       && processing->sh_masking == 0
                       && processing->cs_zone.use_cs == 0;
    if ( config.applySharpen )
    {
        const double s = std::pow(processing->sharpen, 1.5) * 0.55;
        config.sharpenX = s * (1.0 - processing->sharpen_bias);
        config.sharpenY = s * (1.0 + processing->sharpen_bias);
        config.sharpenA = 1.0 + (2.0 * config.sharpenX) + (2.0 * config.sharpenY);
    }

    /* Median denoise: per-pixel square-window median blended by strength/100
     * (denoiser_2d_median.c). Supported for windows up to 5 (winSize<=25); larger
     * windows are rejected (the GLSL order-statistic selection is O(winSize^2)). */
    config.applyMedian = processing->denoiserStrength > 0
                      && processing->denoiserWindow >= 1
                      && processing->denoiserWindow <= 5;
    config.medianWindow = static_cast<int>(processing->denoiserWindow);
    config.medianStrength = static_cast<int>(processing->denoiserStrength);
    if ( config.applyCreativeCurves )
    {
        config.contrastCurveLut = QByteArray(
            reinterpret_cast<const char *>(processing->pre_calc_curve_r),
            static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradationLutY = QByteArray(
            reinterpret_cast<const char *>(processing->gcurve_y),
            static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradationLutR = QByteArray(
            reinterpret_cast<const char *>(processing->gcurve_r),
            static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradationLutG = QByteArray(
            reinterpret_cast<const char *>(processing->gcurve_g),
            static_cast<int>(65536u * sizeof(uint16_t)));
        config.gradationLutB = QByteArray(
            reinterpret_cast<const char *>(processing->gcurve_b),
            static_cast<int>(65536u * sizeof(uint16_t)));
    }

    uint64_t hash = 1469598103934665603ull;
    hash = fnv1a64_append(hash, &config.useCameraMatrix, sizeof(config.useCameraMatrix));
    hash = fnv1a64_append(hash, &config.applyGamutCompression, sizeof(config.applyGamutCompression));
    hash = fnv1a64_append(hash, &config.sourceExposureStops, sizeof(config.sourceExposureStops));
    hash = fnv1a64_append(hash, config.properWbMatrix, sizeof(config.properWbMatrix));
    hash = fnv1a64_append(hash, config.rgbToY, sizeof(config.rgbToY));
    hash = fnv1a64_append(hash, config.levelsLut.constData(), static_cast<size_t>(config.levelsLut.size()));
    hash = fnv1a64_append(hash, config.matrixLutR.constData(), static_cast<size_t>(config.matrixLutR.size()));
    hash = fnv1a64_append(hash, config.matrixLutG.constData(), static_cast<size_t>(config.matrixLutG.size()));
    hash = fnv1a64_append(hash, config.matrixLutB.constData(), static_cast<size_t>(config.matrixLutB.size()));
    hash = fnv1a64_append(hash, config.gammaLut.constData(), static_cast<size_t>(config.gammaLut.size()));
    hash = fnv1a64_append(hash, &config.applyCreativeCurves, sizeof(config.applyCreativeCurves));
    hash = fnv1a64_append(hash, &config.applyToning, sizeof(config.applyToning));
    hash = fnv1a64_append(hash, config.toningGain, sizeof(config.toningGain));
    hash = fnv1a64_append(hash, &config.applyVibrance, sizeof(config.applyVibrance));
    hash = fnv1a64_append(hash, &config.vibrance, sizeof(config.vibrance));
    hash = fnv1a64_append(hash, &config.applySaturation, sizeof(config.applySaturation));
    hash = fnv1a64_append(hash, &config.saturation, sizeof(config.saturation));
    hash = fnv1a64_append(hash, &config.applyHueVs, sizeof(config.applyHueVs));
    hash = fnv1a64_append(hash, &config.applyInLoopContrast, sizeof(config.applyInLoopContrast));
    hash = fnv1a64_append(hash, &config.sourceContrast, sizeof(config.sourceContrast));
    hash = fnv1a64_append(hash, &config.applyAgx, sizeof(config.applyAgx));
    hash = fnv1a64_append(hash, config.agxForward, sizeof(config.agxForward));
    hash = fnv1a64_append(hash, config.agxInverse, sizeof(config.agxInverse));
    hash = fnv1a64_append(hash, &config.applyVignette, sizeof(config.applyVignette));
    hash = fnv1a64_append(hash, &config.vignetteStrength, sizeof(config.vignetteStrength));
    hash = fnv1a64_append(hash, config.vignetteMask.constData(), static_cast<size_t>(config.vignetteMask.size()));
    hash = fnv1a64_append(hash, &config.applyShadowsHighlights, sizeof(config.applyShadowsHighlights));
    hash = fnv1a64_append(hash, &config.shadowsHighlightsCurveIndexMask, sizeof(config.shadowsHighlightsCurveIndexMask));
    hash = fnv1a64_append(hash, config.shadowsHighlightsCurve.constData(), static_cast<size_t>(config.shadowsHighlightsCurve.size()));
    hash = fnv1a64_append(hash, &config.applyLut, sizeof(config.applyLut));
    hash = fnv1a64_append(hash, &config.lut3d, sizeof(config.lut3d));
    hash = fnv1a64_append(hash, &config.lutDimension, sizeof(config.lutDimension));
    hash = fnv1a64_append(hash, &config.lutIntensity, sizeof(config.lutIntensity));
    hash = fnv1a64_append(hash, config.lutDomainMin, sizeof(config.lutDomainMin));
    hash = fnv1a64_append(hash, config.lutDomainMax, sizeof(config.lutDomainMax));
    hash = fnv1a64_append(hash, config.lutCube.constData(), static_cast<size_t>(config.lutCube.size()));
    hash = fnv1a64_append(hash, &config.applyHighlightReconstruction, sizeof(config.applyHighlightReconstruction));
    hash = fnv1a64_append(hash, &config.highlightReconDualIso, sizeof(config.highlightReconDualIso));
    hash = fnv1a64_append(hash, &config.highestGreen, sizeof(config.highestGreen));
    hash = fnv1a64_append(hash, &config.highestGreenDiso, sizeof(config.highestGreenDiso));
    hash = fnv1a64_append(hash, &config.applyGradient, sizeof(config.applyGradient));
    hash = fnv1a64_append(hash, &config.applyGradientContrast, sizeof(config.applyGradientContrast));
    hash = fnv1a64_append(hash, &config.gradientHighestGreen, sizeof(config.gradientHighestGreen));
    hash = fnv1a64_append(hash, &config.gradientHighestGreenDiso, sizeof(config.gradientHighestGreenDiso));
    hash = fnv1a64_append(hash, config.gradientMatrixLutR.constData(), static_cast<size_t>(config.gradientMatrixLutR.size()));
    hash = fnv1a64_append(hash, config.gradientMatrixLutG.constData(), static_cast<size_t>(config.gradientMatrixLutG.size()));
    hash = fnv1a64_append(hash, config.gradientMatrixLutB.constData(), static_cast<size_t>(config.gradientMatrixLutB.size()));
    hash = fnv1a64_append(hash, config.gradientGammaLut.constData(), static_cast<size_t>(config.gradientGammaLut.size()));
    hash = fnv1a64_append(hash, config.gradientContrastCurve.constData(), static_cast<size_t>(config.gradientContrastCurve.size()));
    hash = fnv1a64_append(hash, &config.applyChroma, sizeof(config.applyChroma));
    hash = fnv1a64_append(hash, &config.chromaBlurRadius, sizeof(config.chromaBlurRadius));
    hash = fnv1a64_append(hash, &config.applySharpen, sizeof(config.applySharpen));
    hash = fnv1a64_append(hash, &config.sharpenA, sizeof(config.sharpenA));
    hash = fnv1a64_append(hash, &config.sharpenX, sizeof(config.sharpenX));
    hash = fnv1a64_append(hash, &config.sharpenY, sizeof(config.sharpenY));
    hash = fnv1a64_append(hash, &config.applyMedian, sizeof(config.applyMedian));
    hash = fnv1a64_append(hash, &config.medianWindow, sizeof(config.medianWindow));
    hash = fnv1a64_append(hash, &config.medianStrength, sizeof(config.medianStrength));
    hash = fnv1a64_append(hash, config.inLoopContrastCurve.constData(), static_cast<size_t>(config.inLoopContrastCurve.size()));
    hash = fnv1a64_append(hash, config.hueVsHueCurve.constData(), static_cast<size_t>(config.hueVsHueCurve.size()));
    hash = fnv1a64_append(hash, config.hueVsSaturationCurve.constData(), static_cast<size_t>(config.hueVsSaturationCurve.size()));
    hash = fnv1a64_append(hash, config.hueVsLumaCurve.constData(), static_cast<size_t>(config.hueVsLumaCurve.size()));
    hash = fnv1a64_append(hash, config.lumaVsSaturationCurve.constData(), static_cast<size_t>(config.lumaVsSaturationCurve.size()));
    hash = fnv1a64_append(hash, config.contrastCurveLut.constData(), static_cast<size_t>(config.contrastCurveLut.size()));
    hash = fnv1a64_append(hash, config.gradationLutY.constData(), static_cast<size_t>(config.gradationLutY.size()));
    hash = fnv1a64_append(hash, config.gradationLutR.constData(), static_cast<size_t>(config.gradationLutR.size()));
    hash = fnv1a64_append(hash, config.gradationLutG.constData(), static_cast<size_t>(config.gradationLutG.size()));
    hash = fnv1a64_append(hash, config.gradationLutB.constData(), static_cast<size_t>(config.gradationLutB.size()));
    config.signature = hash;
    return config;
}

bool gpuPreviewProcessingNeedsShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config)
{
    return config.enabled && config.applyShadowsHighlights;
}

bool gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config)
{
    /* The live display shader currently applies only the display LUT family
     * (levels, matrix/WB, gamma). Spatial S/H frame-state is consumed by the
     * offscreen/subset shader, not by display presentation. */
    Q_UNUSED(config);
    return false;
}

bool gpuPreviewProcessingHasShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config,
    int width,
    int height)
{
    if ( !gpuPreviewProcessingNeedsShadowsHighlightsFrameState(config) )
    {
        return true;
    }
    if ( !config.shadowsHighlightsFrameStateReady
      || config.shadowsHighlightsFrameWidth != width
      || config.shadowsHighlightsFrameHeight != height )
    {
        return false;
    }
    const size_t pixelCount =
        static_cast<size_t>(width) * static_cast<size_t>(height);
    return config.shadowsHighlightsBlur.size()
            == static_cast<int>(pixelCount * 3u * sizeof(uint16_t))
        && config.shadowsHighlightsCurve.size()
            == static_cast<int>(65536u * sizeof(float));
}

bool gpuPreviewProcessingAttachFrameState(GpuPreviewProcessingConfig * config,
                                          const processingObject_t * processing,
                                          int width,
                                          int height,
                                          QString * reason)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( config )
        {
            config->shadowsHighlightsFrameStateReady = false;
            config->shadowsHighlightsFrameWidth = 0;
            config->shadowsHighlightsFrameHeight = 0;
            config->shadowsHighlightsBlur.clear();
        }
        if ( reason ) *reason = why;
        return false;
    };

    if ( !config )
    {
        return fail(QStringLiteral("preview-processing config missing"));
    }
    if ( !gpuPreviewProcessingNeedsShadowsHighlightsFrameState(*config) )
    {
        if ( reason ) reason->clear();
        return true;
    }
    if ( width <= 0 || height <= 0 )
    {
        return fail(QStringLiteral("shadows/highlights frame dimensions invalid"));
    }

    const uint16_t * blurData = nullptr;
    int blurWidth = 0;
    int blurHeight = 0;
    int curveIndexMask = 0;
    if ( !processingGetShadowsHighlightsBlurData(processing,
                                                 &blurData,
                                                 &blurWidth,
                                                 &blurHeight,
                                                 &curveIndexMask)
      || !blurData )
    {
        return fail(QStringLiteral("shadows/highlights blur frame unavailable"));
    }
    if ( blurWidth != width || blurHeight != height )
    {
        return fail(QStringLiteral(
            "shadows/highlights blur frame dimensions do not match render frame"));
    }

    const size_t pixelCount =
        static_cast<size_t>(width) * static_cast<size_t>(height);
    const size_t byteCount = pixelCount * 3u * sizeof(uint16_t);
    if ( byteCount > static_cast<size_t>(std::numeric_limits<int>::max()) )
    {
        return fail(QStringLiteral("shadows/highlights blur frame is too large"));
    }

    config->shadowsHighlightsCurveIndexMask = curveIndexMask != 0;
    config->shadowsHighlightsFrameWidth = width;
    config->shadowsHighlightsFrameHeight = height;
    config->shadowsHighlightsBlur =
        QByteArray(reinterpret_cast<const char *>(blurData),
                   static_cast<int>(byteCount));
    config->shadowsHighlightsFrameStateReady = true;
    if ( reason ) reason->clear();
    return true;
}

GpuPreviewProcessingBackendAvailability gpuPreviewProcessingProbeGpuBackend(void)
{
    GpuPreviewProcessingBackendAvailability availability;
    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * glFunctions = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface,
                                              &context,
                                              &glFunctions,
                                              &availability.reason,
                                              &availability.rendererDescription) )
    {
        return availability;
    }

    QOpenGLShaderProgram program;
    QString shaderReason;
    const bool shaderReady = buildSubsetProgram(&program, &shaderReason);
    context.doneCurrent();
    if ( !shaderReady )
    {
        availability.reason = shaderReason;
        return availability;
    }

    availability.available = true;
    availability.reason.clear();
    return availability;
}

void gpuPreviewProcessingApplyCpuReference(const GpuPreviewProcessingConfig & config,
                                           const uint16_t * inputRgb16,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height)
{
    const int pixelCount = width * height;
    if ( !inputRgb16 || !outputRgb16 || pixelCount <= 0 )
    {
        return;
    }

    if ( !config.enabled )
    {
        if ( inputRgb16 != outputRgb16 )
        {
            std::memcpy(outputRgb16,
                        inputRgb16,
                        static_cast<size_t>(pixelCount) * 3u * sizeof(uint16_t));
        }
        return;
    }

    #pragma omp parallel for if(pixelCount >= 2048)
    for (int pixelIndex = 0; pixelIndex < pixelCount; ++pixelIndex)
    {
        const uint16_t * inputPixel = inputRgb16 + pixelIndex * 3;
        uint16_t * outputPixel = outputRgb16 + pixelIndex * 3;
        applyPreviewProcessingPixel(config, inputPixel, outputPixel, pixelIndex);
    }

    /* Spatial post-passes on the developed image (raw_processing.c:1816+). Chroma
     * separation/blur runs in YCbCr space; sharpen is a 5-tap cross. They are
     * mutually exclusive in this subset (the engine interleaves sharpen-on-Y with
     * chroma, which the gate rejects as a combined case). */
    if ( config.applyChroma )
    {
        cpuChromaPostPass(outputRgb16, width, height, config.chromaBlurRadius);
    }
    if ( config.applySharpen )
    {
        cpuSharpenPostPass(outputRgb16, width, height, config.sharpenA, config.sharpenX, config.sharpenY);
    }
    if ( config.applyMedian )
    {
        cpuMedianPostPass(outputRgb16, width, height, config.medianWindow, config.medianStrength);
    }
}

static bool applyChromaPostPassGpu(uint16_t * img, int width, int height, int radius,
                                   QString * reason, QString * rendererDescription);
static bool applySharpenPostPassGpu(uint16_t * img, int width, int height,
                                    double a, double x, double y,
                                    QString * reason, QString * rendererDescription);
static bool applyMedianPostPassGpu(uint16_t * img, int width, int height,
                                   int window, int strength,
                                   QString * reason, QString * rendererDescription);

bool gpuPreviewProcessingApplyGpuOffscreen(const GpuPreviewProcessingConfig & config,
                                           const uint16_t * inputRgb16,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height,
                                           QString * reason,
                                           QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( !inputRgb16 || !outputRgb16 || width <= 0 || height <= 0 )
    {
        return fail(QStringLiteral("preview-processing GPU offscreen input/output buffers are invalid"));
    }

    if ( !config.enabled )
    {
        const int pixelCount = width * height;
        if ( inputRgb16 != outputRgb16 )
        {
            std::memcpy(outputRgb16,
                        inputRgb16,
                        static_cast<size_t>(pixelCount) * 3u * sizeof(uint16_t));
        }
        if ( reason ) reason->clear();
        return true;
    }

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * glFunctions = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface,
                                              &context,
                                              &glFunctions,
                                              reason,
                                              rendererDescription) )
    {
        return false;
    }

    QOpenGLFramebufferObjectFormat fboFormat;
    fboFormat.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fboFormat.setTextureTarget(GL_TEXTURE_2D);
    fboFormat.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fbo(width, height, fboFormat);
    if ( !fbo.isValid() )
    {
        context.doneCurrent();
        return fail(QStringLiteral("QOpenGLFramebufferObject creation failed"));
    }

    QOpenGLShaderProgram program;
    if ( !buildSubsetProgram(&program, reason) )
    {
        context.doneCurrent();
        return false;
    }

    const QByteArray packedFrame = packRgb16Texture(inputRgb16, width * height);
    const QByteArray levelsBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.levelsLut);
    const QByteArray matrixRBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutR);
    const QByteArray matrixGBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutG);
    const QByteArray matrixBBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.matrixLutB);
    const QByteArray gammaBytes = gpuPreviewProcessingPackLookupTextureRgba16(config.gammaLut);
    const QByteArray contrastBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyCreativeCurves ? config.contrastCurveLut : config.gammaLut);
    const QByteArray gradationYBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyCreativeCurves ? config.gradationLutY : config.gammaLut);
    const QByteArray gradationRBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyCreativeCurves ? config.gradationLutR : config.gammaLut);
    const QByteArray gradationGBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyCreativeCurves ? config.gradationLutG : config.gammaLut);
    const QByteArray gradationBBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyCreativeCurves ? config.gradationLutB : config.gammaLut);
    const QByteArray hueVsHueBytes = packCurveTextureR32F(
        config.applyHueVs ? config.hueVsHueCurve : QByteArray());
    const QByteArray hueVsSaturationBytes = packCurveTextureR32F(
        config.applyHueVs ? config.hueVsSaturationCurve : QByteArray());
    const QByteArray hueVsLumaBytes = packCurveTextureR32F(
        config.applyHueVs ? config.hueVsLumaCurve : QByteArray());
    const QByteArray lumaVsSaturationBytes = packCurveTextureR32F(
        config.applyHueVs ? config.lumaVsSaturationCurve : QByteArray());
    const QByteArray inLoopContrastBytes = packContrastCurveR32F(
        config.applyInLoopContrast ? config.inLoopContrastCurve : QByteArray());
    const bool shadowsHighlightsReady =
        gpuPreviewProcessingHasShadowsHighlightsFrameState(config, width, height);
    if ( config.applyShadowsHighlights && !shadowsHighlightsReady )
    {
        context.doneCurrent();
        return fail(QStringLiteral(
            "shadows/highlights frame state missing for GPU preview processing"));
    }
    const bool shadowsHighlightsActiveReady =
        config.applyShadowsHighlights && shadowsHighlightsReady;
    const QByteArray shadowsHighlightsBlurBytes = shadowsHighlightsActiveReady
        ? packRgb16Texture(
            reinterpret_cast<const uint16_t *>(config.shadowsHighlightsBlur.constData()),
            width * height)
        : QByteArray(static_cast<int>(4u * sizeof(uint16_t)), '\0');
    const QByteArray shadowsHighlightsCurveBytes = packContrastCurveR32F(
        config.applyShadowsHighlights ? config.shadowsHighlightsCurve : QByteArray());
    const QByteArray gradMatrixRBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyGradient ? config.gradientMatrixLutR : config.gammaLut);
    const QByteArray gradMatrixGBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyGradient ? config.gradientMatrixLutG : config.gammaLut);
    const QByteArray gradMatrixBBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyGradient ? config.gradientMatrixLutB : config.gammaLut);
    const QByteArray gradGammaBytes = gpuPreviewProcessingPackLookupTextureRgba16(
        config.applyGradient ? config.gradientGammaLut : config.gammaLut);
    const QByteArray gradContrastBytes = packContrastCurveR32F(
        config.applyGradientContrast ? config.gradientContrastCurve : QByteArray());

    QOpenGLTexture * frameTexture = createFrameTexture(width, height);
    QOpenGLTexture * levelsTexture = createLookupTexture();
    QOpenGLTexture * matrixRTexture = createLookupTexture();
    QOpenGLTexture * matrixGTexture = createLookupTexture();
    QOpenGLTexture * matrixBTexture = createLookupTexture();
    QOpenGLTexture * gammaTexture = createLookupTexture();
    QOpenGLTexture * contrastCurveTexture = createLookupTexture();
    QOpenGLTexture * gradationYTexture = createLookupTexture();
    QOpenGLTexture * gradationRTexture = createLookupTexture();
    QOpenGLTexture * gradationGTexture = createLookupTexture();
    QOpenGLTexture * gradationBTexture = createLookupTexture();
    QOpenGLTexture * hueVsHueTexture = createCurveTexture();
    QOpenGLTexture * hueVsSaturationTexture = createCurveTexture();
    QOpenGLTexture * hueVsLumaTexture = createCurveTexture();
    QOpenGLTexture * lumaVsSaturationTexture = createCurveTexture();
    QOpenGLTexture * inLoopContrastTexture = createContrastCurveTexture();
    QOpenGLTexture * shadowsHighlightsBlurTexture = createFrameTexture(
        shadowsHighlightsActiveReady ? width : 1,
        shadowsHighlightsActiveReady ? height : 1);
    QOpenGLTexture * shadowsHighlightsCurveTexture = createContrastCurveTexture();
    const bool vignetteReady = config.applyVignette
        && config.vignetteMask.size() == static_cast<int>(static_cast<size_t>(width) * height * sizeof(float));
    QOpenGLTexture * vignetteMaskTexture = createVignetteMaskTexture(
        vignetteReady ? width : 1, vignetteReady ? height : 1);
    const bool lutActive = config.applyLut && config.lutDimension > 1;
    const bool lut3dActive = lutActive && config.lut3d;
    const bool lut1dActive = lutActive && !config.lut3d;
    QOpenGLTexture * lut3dTexture = createLut3dTexture(lut3dActive ? config.lutDimension : 1);
    QOpenGLTexture * lut1dTexture = createLut1dTexture(lut1dActive ? config.lutDimension : 1);
    QOpenGLTexture * gradMatrixRTexture = createLookupTexture();
    QOpenGLTexture * gradMatrixGTexture = createLookupTexture();
    QOpenGLTexture * gradMatrixBTexture = createLookupTexture();
    QOpenGLTexture * gradGammaTexture = createLookupTexture();
    QOpenGLTexture * gradContrastTexture = createContrastCurveTexture();
    const bool gradientReady = config.applyGradient && config.gradientMaskData != nullptr;
    QByteArray gradientMaskBytes;
    if ( gradientReady )
    {
        gradientMaskBytes.resize(static_cast<int>(static_cast<size_t>(width) * height * sizeof(float)));
        float * gmDst = reinterpret_cast<float *>(gradientMaskBytes.data());
        for (int i = 0; i < width * height; ++i)
        {
            gmDst[i] = static_cast<float>(config.gradientMaskData[i]);
        }
    }
    else
    {
        gradientMaskBytes = QByteArray(static_cast<int>(sizeof(float)), '\0');
    }
    QOpenGLTexture * gradientMaskTexture = createVignetteMaskTexture(
        gradientReady ? width : 1, gradientReady ? height : 1);

    QOpenGLTexture * previewTextures[] = {
        frameTexture, levelsTexture, matrixRTexture, matrixGTexture, matrixBTexture,
        gammaTexture, contrastCurveTexture, gradationYTexture, gradationRTexture,
        gradationGTexture, gradationBTexture, hueVsHueTexture, hueVsSaturationTexture,
        hueVsLumaTexture, lumaVsSaturationTexture, inLoopContrastTexture,
        shadowsHighlightsBlurTexture, shadowsHighlightsCurveTexture, vignetteMaskTexture,
        lut3dTexture, lut1dTexture, gradMatrixRTexture, gradMatrixGTexture,
        gradMatrixBTexture, gradGammaTexture, gradContrastTexture, gradientMaskTexture
    };
    for (QOpenGLTexture * texture : previewTextures)
    {
        if ( !previewProcessingTextureIsReady(texture) )
        {
            for (QOpenGLTexture * allocated : previewTextures) delete allocated;
            context.doneCurrent();
            return fail(QStringLiteral("offscreen preview texture creation/storage failed"));
        }
    }

    frameTexture->setData(QOpenGLTexture::RGBA,
                          QOpenGLTexture::UInt16,
                          packedFrame.constData());
    levelsTexture->setData(QOpenGLTexture::RGBA,
                           QOpenGLTexture::UInt16,
                           levelsBytes.constData());
    matrixRTexture->setData(QOpenGLTexture::RGBA,
                            QOpenGLTexture::UInt16,
                            matrixRBytes.constData());
    matrixGTexture->setData(QOpenGLTexture::RGBA,
                            QOpenGLTexture::UInt16,
                            matrixGBytes.constData());
    matrixBTexture->setData(QOpenGLTexture::RGBA,
                            QOpenGLTexture::UInt16,
                            matrixBBytes.constData());
    gammaTexture->setData(QOpenGLTexture::RGBA,
                          QOpenGLTexture::UInt16,
                          gammaBytes.constData());
    contrastCurveTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, contrastBytes.constData());
    gradationYTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradationYBytes.constData());
    gradationRTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradationRBytes.constData());
    gradationGTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradationGBytes.constData());
    gradationBTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradationBBytes.constData());
    hueVsHueTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, hueVsHueBytes.constData());
    hueVsSaturationTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, hueVsSaturationBytes.constData());
    hueVsLumaTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, hueVsLumaBytes.constData());
    lumaVsSaturationTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, lumaVsSaturationBytes.constData());
    inLoopContrastTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, inLoopContrastBytes.constData());
    shadowsHighlightsBlurTexture->setData(QOpenGLTexture::RGBA,
                                          QOpenGLTexture::UInt16,
                                          shadowsHighlightsBlurBytes.constData());
    shadowsHighlightsCurveTexture->setData(QOpenGLTexture::Red,
                                           QOpenGLTexture::Float32,
                                           shadowsHighlightsCurveBytes.constData());
    const float vignetteDummy = 0.0f;
    vignetteMaskTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32,
                                 vignetteReady ? config.vignetteMask.constData()
                                               : reinterpret_cast<const char *>(&vignetteDummy));
    const QByteArray lutDummy(4 * static_cast<int>(sizeof(float)), '\0');
    const QByteArray lut3dBytes = lut3dActive
        ? packLutCubeRgba32F(config.lutCube, config.lutDimension * config.lutDimension * config.lutDimension)
        : lutDummy;
    const QByteArray lut1dBytes = lut1dActive
        ? packLutCubeRgba32F(config.lutCube, config.lutDimension)
        : lutDummy;
    lut3dTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, lut3dBytes.constData());
    lut1dTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, lut1dBytes.constData());
    gradMatrixRTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradMatrixRBytes.constData());
    gradMatrixGTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradMatrixGBytes.constData());
    gradMatrixBTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradMatrixBBytes.constData());
    gradGammaTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, gradGammaBytes.constData());
    gradContrastTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, gradContrastBytes.constData());
    gradientMaskTexture->setData(QOpenGLTexture::Red, QOpenGLTexture::Float32, gradientMaskBytes.constData());

    if ( !fbo.bind() )
    {
        context.doneCurrent();
        return fail(QStringLiteral("offscreen preview framebuffer bind failed"));
    }
    glFunctions->glViewport(0, 0, width, height);
    glFunctions->glDisable(GL_DEPTH_TEST);
    glFunctions->glDisable(GL_BLEND);
    glFunctions->glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glFunctions->glClear(GL_COLOR_BUFFER_BIT);

    program.bind();
    program.setUniformValue("frameTexture", 0);
    program.setUniformValue("levelsLut", 1);
    program.setUniformValue("matrixLutR", 2);
    program.setUniformValue("matrixLutG", 3);
    program.setUniformValue("matrixLutB", 4);
    program.setUniformValue("gammaLut", 5);
    program.setUniformValue("contrastCurveLut", 6);
    program.setUniformValue("gradationLutY", 7);
    program.setUniformValue("gradationLutR", 8);
    program.setUniformValue("gradationLutG", 9);
    program.setUniformValue("gradationLutB", 10);
    program.setUniformValue("hueVsHueCurve", 11);
    program.setUniformValue("hueVsSaturationCurve", 12);
    program.setUniformValue("hueVsLumaCurve", 13);
    program.setUniformValue("lumaVsSaturationCurve", 14);
    program.setUniformValue("inLoopContrastCurve", 15);
    program.setUniformValue("shadowsHighlightsBlurTexture", 25);
    program.setUniformValue("shadowsHighlightsCurve", 26);
    program.setUniformValue("vignetteMask", 16);
    program.setUniformValue("previewApplyVignette", vignetteReady ? 1.0f : 0.0f);
    program.setUniformValue("previewVignetteStrength", static_cast<float>(config.vignetteStrength));
    program.setUniformValue("frameSize", QVector2D(static_cast<float>(width), static_cast<float>(height)));
    program.setUniformValue("lut3dTexture", 17);
    program.setUniformValue("lut1dTexture", 18);
    program.setUniformValue("previewApplyLut", lutActive ? 1.0f : 0.0f);
    program.setUniformValue("previewLut3d", lut3dActive ? 1.0f : 0.0f);
    program.setUniformValue("previewLutDimension", static_cast<float>(config.lutDimension));
    program.setUniformValue("previewLutIntensity", config.lutIntensity);
    program.setUniformValue("previewLutDomainMin", QVector3D(config.lutDomainMin[0], config.lutDomainMin[1], config.lutDomainMin[2]));
    program.setUniformValue("previewLutDomainMax", QVector3D(config.lutDomainMax[0], config.lutDomainMax[1], config.lutDomainMax[2]));
    program.setUniformValue("previewApplyHueVs", config.applyHueVs ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyInLoopContrast", config.applyInLoopContrast ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyShadowsHighlights",
                            shadowsHighlightsActiveReady ? 1.0f : 0.0f);
    program.setUniformValue("previewShadowsHighlightsCurveIndexMask",
                            config.shadowsHighlightsCurveIndexMask ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyAgx", config.applyAgx ? 1.0f : 0.0f);
    program.setUniformValue("previewAgxFwd0", QVector3D(config.agxForward[0], config.agxForward[1], config.agxForward[2]));
    program.setUniformValue("previewAgxFwd1", QVector3D(config.agxForward[3], config.agxForward[4], config.agxForward[5]));
    program.setUniformValue("previewAgxFwd2", QVector3D(config.agxForward[6], config.agxForward[7], config.agxForward[8]));
    program.setUniformValue("previewAgxInv0", QVector3D(config.agxInverse[0], config.agxInverse[1], config.agxInverse[2]));
    program.setUniformValue("previewAgxInv1", QVector3D(config.agxInverse[3], config.agxInverse[4], config.agxInverse[5]));
    program.setUniformValue("previewAgxInv2", QVector3D(config.agxInverse[6], config.agxInverse[7], config.agxInverse[8]));
    program.setUniformValue("previewApplyCreativeCurves", config.applyCreativeCurves ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyToning", config.applyToning ? 1.0f : 0.0f);
    program.setUniformValue("previewToningGain",
                            QVector3D(config.toningGain[0], config.toningGain[1], config.toningGain[2]));
    program.setUniformValue("previewApplyVibrance", config.applyVibrance ? 1.0f : 0.0f);
    program.setUniformValue("previewVibrance", config.vibrance);
    program.setUniformValue("previewApplySaturation", config.applySaturation ? 1.0f : 0.0f);
    program.setUniformValue("previewSaturation", config.saturation);
    program.setUniformValue("previewProcessingEnabled", config.enabled ? 1.0f : 0.0f);
    program.setUniformValue("previewUseCameraMatrix", config.useCameraMatrix ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyGamutCompression", config.applyGamutCompression ? 1.0f : 0.0f);
    program.setUniformValue("previewProperWbRow0",
                            QVector3D(config.properWbMatrix[0],
                                      config.properWbMatrix[1],
                                      config.properWbMatrix[2]));
    program.setUniformValue("previewProperWbRow1",
                            QVector3D(config.properWbMatrix[3],
                                      config.properWbMatrix[4],
                                      config.properWbMatrix[5]));
    program.setUniformValue("previewProperWbRow2",
                            QVector3D(config.properWbMatrix[6],
                                      config.properWbMatrix[7],
                                      config.properWbMatrix[8]));
    program.setUniformValue("previewRgbToY",
                            QVector3D(config.rgbToY[0],
                                      config.rgbToY[1],
                                      config.rgbToY[2]));
    program.setUniformValue("previewApplyHighlightRecon", config.applyHighlightReconstruction ? 1.0f : 0.0f);
    program.setUniformValue("previewHighlightReconDualIso", config.highlightReconDualIso ? 1.0f : 0.0f);
    program.setUniformValue("previewHighestGreen", static_cast<float>(config.highestGreen));
    program.setUniformValue("previewHighestGreenDiso", static_cast<float>(config.highestGreenDiso));
    program.setUniformValue("gradMatrixLutR", 19);
    program.setUniformValue("gradMatrixLutG", 20);
    program.setUniformValue("gradMatrixLutB", 21);
    program.setUniformValue("gradGammaLut", 22);
    program.setUniformValue("gradientContrastCurve", 23);
    program.setUniformValue("gradientMask", 24);
    program.setUniformValue("previewApplyGradient", gradientReady ? 1.0f : 0.0f);
    program.setUniformValue("previewApplyGradientContrast", config.applyGradientContrast ? 1.0f : 0.0f);
    program.setUniformValue("previewGradientHighestGreen", static_cast<float>(config.gradientHighestGreen));
    program.setUniformValue("previewGradientHighestGreenDiso", static_cast<float>(config.gradientHighestGreenDiso));

    frameTexture->bind(0);
    levelsTexture->bind(1);
    matrixRTexture->bind(2);
    matrixGTexture->bind(3);
    matrixBTexture->bind(4);
    gammaTexture->bind(5);
    contrastCurveTexture->bind(6);
    gradationYTexture->bind(7);
    gradationRTexture->bind(8);
    gradationGTexture->bind(9);
    gradationBTexture->bind(10);
    hueVsHueTexture->bind(11);
    hueVsSaturationTexture->bind(12);
    hueVsLumaTexture->bind(13);
    lumaVsSaturationTexture->bind(14);
    inLoopContrastTexture->bind(15);
    vignetteMaskTexture->bind(16);
    lut3dTexture->bind(17);
    lut1dTexture->bind(18);
    gradMatrixRTexture->bind(19);
    gradMatrixGTexture->bind(20);
    gradMatrixBTexture->bind(21);
    gradGammaTexture->bind(22);
    gradContrastTexture->bind(23);
    gradientMaskTexture->bind(24);
    shadowsHighlightsBlurTexture->bind(25);
    shadowsHighlightsCurveTexture->bind(26);

    const int posLoc = program.attributeLocation("position");
    const int texLoc = program.attributeLocation("texCoord");
    program.enableAttributeArray(posLoc);
    program.enableAttributeArray(texLoc);
    program.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
    program.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
    glFunctions->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glFunctions->glFinish();

    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    glFunctions->glReadPixels(0,
                              0,
                              width,
                              height,
                              GL_RGBA,
                              GL_UNSIGNED_SHORT,
                              readback.data());

    unpackRgb16Readback(readback, outputRgb16, width, height);

    program.disableAttributeArray(posLoc);
    program.disableAttributeArray(texLoc);
    frameTexture->release();
    levelsTexture->release();
    matrixRTexture->release();
    matrixGTexture->release();
    matrixBTexture->release();
    gammaTexture->release();
    contrastCurveTexture->release();
    gradationYTexture->release();
    gradationRTexture->release();
    gradationGTexture->release();
    gradationBTexture->release();
    hueVsHueTexture->release();
    hueVsSaturationTexture->release();
    hueVsLumaTexture->release();
    lumaVsSaturationTexture->release();
    inLoopContrastTexture->release();
    vignetteMaskTexture->release();
    lut3dTexture->release();
    lut1dTexture->release();
    gradMatrixRTexture->release();
    gradMatrixGTexture->release();
    gradMatrixBTexture->release();
    gradGammaTexture->release();
    gradContrastTexture->release();
    gradientMaskTexture->release();
    shadowsHighlightsBlurTexture->release();
    shadowsHighlightsCurveTexture->release();
    program.release();
    fbo.release();

    delete frameTexture;
    delete levelsTexture;
    delete matrixRTexture;
    delete matrixGTexture;
    delete matrixBTexture;
    delete gammaTexture;
    delete contrastCurveTexture;
    delete gradationYTexture;
    delete gradationRTexture;
    delete gradationGTexture;
    delete gradationBTexture;
    delete hueVsHueTexture;
    delete hueVsSaturationTexture;
    delete hueVsLumaTexture;
    delete lumaVsSaturationTexture;
    delete inLoopContrastTexture;
    delete vignetteMaskTexture;
    delete lut3dTexture;
    delete lut1dTexture;
    delete gradMatrixRTexture;
    delete gradMatrixGTexture;
    delete gradMatrixBTexture;
    delete gradGammaTexture;
    delete gradContrastTexture;
    delete gradientMaskTexture;
    delete shadowsHighlightsBlurTexture;
    delete shadowsHighlightsCurveTexture;
    context.doneCurrent();

    /* Spatial post-pass on the developed image, in its own GL context (the per-
     * pixel pass above has released this one). Chroma separation/blur reproduces
     * raw_processing.c:1816-1971. */
    if ( config.applyChroma )
    {
        QString chromaReason;
        QString chromaRenderer;
        if ( !applyChromaPostPassGpu(outputRgb16, width, height, config.chromaBlurRadius,
                                     &chromaReason, &chromaRenderer) )
        {
            return fail(chromaReason);
        }
        if ( rendererDescription && !chromaRenderer.isEmpty() )
        {
            *rendererDescription = chromaRenderer;
        }
    }
    if ( config.applySharpen )
    {
        QString sharpenReason;
        QString sharpenRenderer;
        if ( !applySharpenPostPassGpu(outputRgb16, width, height,
                                      config.sharpenA, config.sharpenX, config.sharpenY,
                                      &sharpenReason, &sharpenRenderer) )
        {
            return fail(sharpenReason);
        }
        if ( rendererDescription && !sharpenRenderer.isEmpty() )
        {
            *rendererDescription = sharpenRenderer;
        }
    }
    if ( config.applyMedian )
    {
        QString medianReason;
        QString medianRenderer;
        if ( !applyMedianPostPassGpu(outputRgb16, width, height,
                                     config.medianWindow, config.medianStrength,
                                     &medianReason, &medianRenderer) )
        {
            return fail(medianReason);
        }
        if ( rendererDescription && !medianRenderer.isEmpty() )
        {
            *rendererDescription = medianRenderer;
        }
    }

    if ( reason ) reason->clear();
    return true;
}

QByteArray gpuPreviewProcessingBoxBlurFragmentShaderSource(void)
{
    /* One separable box-blur pass (axis chosen by `horizontal`). Each fragment
     * sums the (2*radius+1)-tap window of EXACT integer texel values, divides by
     * the diameter with a truncating floor (matching the engine's integer
     * sum/blur_diameter), and clamps the tap coordinate to the edge. Disabled
     * channels pass the center texel through unchanged (blur_image leaves
     * do_*=0 channels untouched). Radius<=127 keeps the window sum < 2^24 so the
     * float32 accumulation is exact. */
    return QByteArrayLiteral(
        "uniform sampler2D src;\n"
        "uniform vec2 texSize;\n"
        "uniform float radius;\n"
        "uniform float horizontal;\n"
        "uniform vec3 channelMask;\n"
        "varying vec2 vTexCoord;\n"
        "void main()\n"
        "{\n"
        "    vec2 px = floor(vTexCoord * texSize);\n"
        "    float diameter = 2.0 * radius + 1.0;\n"
        "    vec3 center = floor(texture2D(src, (px + vec2(0.5)) / texSize).rgb * 65535.0 + 0.5);\n"
        "    vec3 sum = vec3(0.0);\n"
        "    for (int k = 0; k < 255; k++)\n"
        "    {\n"
        "        if (float(k) > 2.0 * radius) break;\n"
        "        /* Engine blur_image window for output j is [j-r+1, j+r+1] -- a\n"
        "         * centered box shifted right by exactly one pixel (its off-by-one\n"
        "         * quirk), clamped to the edge. Reproduce that shift here. */\n"
        "        float off = float(k) - radius + 1.0;\n"
        "        vec2 c = px;\n"
        "        if (horizontal > 0.5) c.x = clamp(px.x + off, 0.0, texSize.x - 1.0);\n"
        "        else c.y = clamp(px.y + off, 0.0, texSize.y - 1.0);\n"
        "        sum += floor(texture2D(src, (c + vec2(0.5)) / texSize).rgb * 65535.0 + 0.5);\n"
        "    }\n"
        "    vec3 blurred = floor(sum / diameter);\n"
        "    vec3 outv = mix(center, blurred, channelMask);\n"
        "    gl_FragColor = vec4(outv / 65535.0, 1.0);\n"
        "}\n");
}

bool gpuPreviewProcessingApplyBoxBlurOffscreen(const uint16_t * inputRgb16,
                                               uint16_t * outputRgb16,
                                               int width,
                                               int height,
                                               int radius,
                                               bool doR,
                                               bool doG,
                                               bool doB,
                                               QString * reason,
                                               QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( !inputRgb16 || !outputRgb16 || width <= 0 || height <= 0 )
    {
        return fail(QStringLiteral("box blur input/output buffers are invalid"));
    }
    if ( radius < 0 )
    {
        return fail(QStringLiteral("box blur radius is negative"));
    }
    if ( radius > 127 )
    {
        return fail(QStringLiteral("box blur radius exceeds the float32-exact limit (127)"));
    }
    if ( radius == 0 )
    {
        std::memcpy(outputRgb16, inputRgb16,
                    static_cast<size_t>(width) * height * 3u * sizeof(uint16_t));
        if ( reason ) reason->clear();
        return true;
    }

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * glFunctions = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface, &context, &glFunctions,
                                              reason, rendererDescription) )
    {
        return false;
    }

    QOpenGLShaderProgram program;
    {
        const QByteArray vertexShader = gpuPreviewProcessingVertexShaderSource();
        const QByteArray fragmentShader = gpuPreviewProcessingBoxBlurFragmentShaderSource();
        if ( !program.addShaderFromSourceCode(QOpenGLShader::Vertex, vertexShader)
          || !program.addShaderFromSourceCode(QOpenGLShader::Fragment, fragmentShader)
          || !program.link() )
        {
            const QString log = program.log();
            context.doneCurrent();
            return fail(QStringLiteral("box blur shader setup failed: %1").arg(log));
        }
    }

    const QByteArray packedFrame = packRgb16Texture(inputRgb16, width * height);
    QOpenGLTexture * srcTexture = createFrameTexture(width, height);
    if ( !previewProcessingTextureIsReady(srcTexture) )
    {
        delete srcTexture;
        context.doneCurrent();
        return fail(QStringLiteral("box blur source texture creation/storage failed"));
    }
    srcTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, packedFrame.constData());

    QOpenGLFramebufferObjectFormat fboFormat;
    fboFormat.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fboFormat.setTextureTarget(GL_TEXTURE_2D);
    fboFormat.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fboHorizontal(width, height, fboFormat);
    QOpenGLFramebufferObject fboVertical(width, height, fboFormat);
    if ( !fboHorizontal.isValid() || !fboVertical.isValid() )
    {
        delete srcTexture;
        context.doneCurrent();
        return fail(QStringLiteral("box blur framebuffer creation failed"));
    }

    const int posLoc = program.attributeLocation("position");
    const int texLoc = program.attributeLocation("texCoord");

    auto drawPass = [&](GLuint inputTexture, QOpenGLFramebufferObject & target, float horizontal)
        -> bool
    {
        if ( !target.bind() ) return false;
        glFunctions->glViewport(0, 0, width, height);
        glFunctions->glDisable(GL_DEPTH_TEST);
        glFunctions->glDisable(GL_BLEND);
        glFunctions->glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        glFunctions->glClear(GL_COLOR_BUFFER_BIT);
        program.bind();
        program.setUniformValue("src", 0);
        program.setUniformValue("texSize", QVector2D(static_cast<float>(width), static_cast<float>(height)));
        program.setUniformValue("radius", static_cast<float>(radius));
        program.setUniformValue("horizontal", horizontal);
        program.setUniformValue("channelMask",
                                QVector3D(doR ? 1.0f : 0.0f, doG ? 1.0f : 0.0f, doB ? 1.0f : 0.0f));
        glFunctions->glActiveTexture(GL_TEXTURE0);
        glFunctions->glBindTexture(GL_TEXTURE_2D, inputTexture);
        glFunctions->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glFunctions->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glFunctions->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glFunctions->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        program.enableAttributeArray(posLoc);
        program.enableAttributeArray(texLoc);
        program.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
        program.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
        glFunctions->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        glFunctions->glFinish();
        program.disableAttributeArray(posLoc);
        program.disableAttributeArray(texLoc);
        target.release();
        return true;
    };

    /* Horizontal pass into fboHorizontal, then vertical pass reading it into
     * fboVertical. The passes do NOT flip Y (unlike the main preview offscreen),
     * so readback row 0 maps to input row 0 -- the box is symmetric and the
     * edge clamp is position-dependent, so the orientation must match blur_image. */
    if ( !drawPass(srcTexture->textureId(), fboHorizontal, 1.0f)
      || !drawPass(fboHorizontal.texture(), fboVertical, 0.0f) )
    {
        program.release();
        delete srcTexture;
        context.doneCurrent();
        return fail(QStringLiteral("box blur framebuffer bind failed"));
    }

    if ( !fboVertical.bind() )
    {
        program.release();
        delete srcTexture;
        context.doneCurrent();
        return fail(QStringLiteral("box blur framebuffer readback bind failed"));
    }
    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    glFunctions->glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_SHORT, readback.data());
    fboVertical.release();

    const uint16_t * pixels = reinterpret_cast<const uint16_t *>(readback.constData());
    for (int i = 0; i < width * height; ++i)
    {
        outputRgb16[i * 3 + 0] = pixels[i * 4 + 0];
        outputRgb16[i * 3 + 1] = pixels[i * 4 + 1];
        outputRgb16[i * 3 + 2] = pixels[i * 4 + 2];
    }

    program.release();
    delete srcTexture;
    context.doneCurrent();

    if ( reason ) reason->clear();
    return true;
}

namespace
{
QByteArray chromaForwardShaderSource()
{
    /* RGB -> YCbCr (JPEG transform, raw_processing.c convert_rgb_to_YCbCr_omp).
     * Each term is read from an RGBA32F LUT holding the engine's truncated-int
     * products; the 0.5 R/B terms are integer >>1 (floor(v/2)). */
    return QByteArrayLiteral(
        "uniform sampler2D src;\n"
        "uniform sampler2D fwdR;\n"
        "uniform sampler2D fwdG;\n"
        "uniform sampler2D fwdB;\n"
        "varying vec2 vTexCoord;\n"
        "vec3 chromaLut(sampler2D lut, float v)\n"
        "{\n"
        "    float i = clamp(v, 0.0, 65535.0);\n"
        "    vec2 uv = (vec2(mod(i, 256.0), floor(i / 256.0)) + vec2(0.5)) / 256.0;\n"
        "    return texture2D(lut, uv).rgb;\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec3 rgb = floor(texture2D(src, vTexCoord).rgb * 65535.0 + 0.5);\n"
        "    vec3 fr = chromaLut(fwdR, rgb.r);\n"
        "    vec3 fg = chromaLut(fwdG, rgb.g);\n"
        "    vec3 fb = chromaLut(fwdB, rgb.b);\n"
        "    float Y  = fr.r + fg.r + fb.r;\n"
        "    float Cb = 32768.0 + fr.g + fg.g + floor(rgb.b / 2.0);\n"
        "    float Cr = 32768.0 + floor(rgb.r / 2.0) + fg.b + fb.g;\n"
        "    vec3 ycc = clamp(vec3(Y, Cb, Cr), 0.0, 65535.0);\n"
        "    gl_FragColor = vec4(ycc / 65535.0, 1.0);\n"
        "}\n");
}

QByteArray chromaInverseShaderSource()
{
    /* YCbCr -> RGB (raw_processing.c convert_YCbCr_to_rgb_omp). */
    return QByteArrayLiteral(
        "uniform sampler2D src;\n"
        "uniform sampler2D invCr;\n"
        "uniform sampler2D invCb;\n"
        "varying vec2 vTexCoord;\n"
        "vec3 chromaLut(sampler2D lut, float v)\n"
        "{\n"
        "    float i = clamp(v, 0.0, 65535.0);\n"
        "    vec2 uv = (vec2(mod(i, 256.0), floor(i / 256.0)) + vec2(0.5)) / 256.0;\n"
        "    return texture2D(lut, uv).rgb;\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec3 ycc = floor(texture2D(src, vTexCoord).rgb * 65535.0 + 0.5);\n"
        "    vec3 icr = chromaLut(invCr, ycc.b);\n"
        "    vec3 icb = chromaLut(invCb, ycc.g);\n"
        "    float R = ycc.r + icr.r;\n"
        "    float G = ycc.r + icb.r + icr.g;\n"
        "    float B = ycc.r + icb.g;\n"
        "    gl_FragColor = vec4(clamp(vec3(R, G, B), 0.0, 65535.0) / 65535.0, 1.0);\n"
        "}\n");
}

/* 256x256 RGBA32F LUT: texel[j] = ((int)((j-bias)*cR), (int)((j-bias)*cG),
 * (int)((j-bias)*cB), 0), matching the engine cs_zone LUT build
 * ((int32)((double)j*coeff)). bias=0 forward, 32768 inverse. */
QByteArray packChromaLutRgba32F(double cR, double cG, double cB, int bias)
{
    QByteArray packed(256 * 256 * 4 * static_cast<int>(sizeof(float)), Qt::Uninitialized);
    float * dst = reinterpret_cast<float *>(packed.data());
    for (int j = 0; j < 65536; ++j)
    {
        const double d = static_cast<double>(j - bias);
        dst[j * 4 + 0] = static_cast<float>(static_cast<int>(d * cR));
        dst[j * 4 + 1] = static_cast<float>(static_cast<int>(d * cG));
        dst[j * 4 + 2] = static_cast<float>(static_cast<int>(d * cB));
        dst[j * 4 + 3] = 0.0f;
    }
    return packed;
}

QOpenGLTexture * createChromaLutTexture()
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA32F);
    texture->setSize(256, 256);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::Float32);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}
}

static bool applyChromaPostPassGpu(uint16_t * img, int width, int height, int radius,
                                   QString * reason, QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool { if ( reason ) *reason = why; return false; };
    if ( !img || width <= 0 || height <= 0 ) return fail(QStringLiteral("chroma post-pass invalid buffer"));

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * gl = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface, &context, &gl, reason, rendererDescription) )
        return false;

    QOpenGLShaderProgram forwardProgram, inverseProgram, blurProgram;
    const QByteArray vs = gpuPreviewProcessingVertexShaderSource();
    auto buildProg = [&](QOpenGLShaderProgram & p, const QByteArray & fs) -> bool {
        return p.addShaderFromSourceCode(QOpenGLShader::Vertex, vs)
            && p.addShaderFromSourceCode(QOpenGLShader::Fragment, fs)
            && p.link();
    };
    if ( !buildProg(forwardProgram, chromaForwardShaderSource())
      || !buildProg(inverseProgram, chromaInverseShaderSource())
      || !buildProg(blurProgram, gpuPreviewProcessingBoxBlurFragmentShaderSource()) )
    {
        context.doneCurrent();
        return fail(QStringLiteral("chroma shader setup failed"));
    }

    const QByteArray packedFrame = packRgb16Texture(img, width * height);
    QOpenGLTexture * srcTexture = createFrameTexture(width, height);
    srcTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, packedFrame.constData());

    QOpenGLTexture * fwdR = createChromaLutTexture();
    QOpenGLTexture * fwdG = createChromaLutTexture();
    QOpenGLTexture * fwdB = createChromaLutTexture();
    QOpenGLTexture * invCr = createChromaLutTexture();
    QOpenGLTexture * invCb = createChromaLutTexture();
    QOpenGLTexture * chromaTextures[] = { srcTexture, fwdR, fwdG, fwdB, invCr, invCb };
    for (QOpenGLTexture * texture : chromaTextures)
    {
        if ( !previewProcessingTextureIsReady(texture) )
        {
            for (QOpenGLTexture * allocated : chromaTextures) delete allocated;
            context.doneCurrent();
            return fail(QStringLiteral("chroma texture creation/storage failed"));
        }
    }
    {
        const QByteArray a = packChromaLutRgba32F(0.299, -0.168736, 0.0, 0);
        const QByteArray b = packChromaLutRgba32F(0.587, -0.331264, -0.418688, 0);
        const QByteArray c = packChromaLutRgba32F(0.114, -0.081312, 0.0, 0);
        const QByteArray d = packChromaLutRgba32F(1.402, -0.714136, 0.0, 32768);
        const QByteArray e = packChromaLutRgba32F(-0.344136, 1.772, 0.0, 32768);
        fwdR->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, a.constData());
        fwdG->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, b.constData());
        fwdB->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, c.constData());
        invCr->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, d.constData());
        invCb->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, e.constData());
    }

    QOpenGLFramebufferObjectFormat fmt;
    fmt.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fmt.setTextureTarget(GL_TEXTURE_2D);
    fmt.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fboA(width, height, fmt);
    QOpenGLFramebufferObject fboB(width, height, fmt);
    QOpenGLFramebufferObject fboC(width, height, fmt);
    if ( !fboA.isValid() || !fboB.isValid() || !fboC.isValid() )
    {
        delete srcTexture; delete fwdR; delete fwdG; delete fwdB; delete invCr; delete invCb;
        context.doneCurrent();
        return fail(QStringLiteral("chroma framebuffer creation failed"));
    }

    auto drawQuad = [&](QOpenGLShaderProgram & p) {
        const int posLoc = p.attributeLocation("position");
        const int texLoc = p.attributeLocation("texCoord");
        p.enableAttributeArray(posLoc);
        p.enableAttributeArray(texLoc);
        p.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
        p.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
        gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        gl->glFinish();
        p.disableAttributeArray(posLoc);
        p.disableAttributeArray(texLoc);
    };
    auto beginPass = [&](QOpenGLFramebufferObject & fbo) {
        fbo.bind();
        gl->glViewport(0, 0, width, height);
        gl->glDisable(GL_DEPTH_TEST); gl->glDisable(GL_BLEND);
        gl->glClearColor(0.0f, 0.0f, 0.0f, 1.0f); gl->glClear(GL_COLOR_BUFFER_BIT);
    };

    beginPass(fboA);
    forwardProgram.bind();
    forwardProgram.setUniformValue("src", 0);
    forwardProgram.setUniformValue("fwdR", 1);
    forwardProgram.setUniformValue("fwdG", 2);
    forwardProgram.setUniformValue("fwdB", 3);
    gl->glActiveTexture(GL_TEXTURE0); gl->glBindTexture(GL_TEXTURE_2D, srcTexture->textureId());
    fwdR->bind(1); fwdG->bind(2); fwdB->bind(3);
    drawQuad(forwardProgram);
    forwardProgram.release();
    fboA.release();

    GLuint ycctex = fboA.texture();
    if ( radius > 0 )
    {
        auto blurPass = [&](GLuint inTex, QOpenGLFramebufferObject & target, float horizontal) {
            beginPass(target);
            blurProgram.bind();
            blurProgram.setUniformValue("src", 0);
            blurProgram.setUniformValue("texSize", QVector2D(static_cast<float>(width), static_cast<float>(height)));
            blurProgram.setUniformValue("radius", static_cast<float>(radius));
            blurProgram.setUniformValue("horizontal", horizontal);
            blurProgram.setUniformValue("channelMask", QVector3D(0.0f, 1.0f, 1.0f));
            gl->glActiveTexture(GL_TEXTURE0); gl->glBindTexture(GL_TEXTURE_2D, inTex);
            gl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            gl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            gl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            gl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
            drawQuad(blurProgram);
            blurProgram.release();
            target.release();
        };
        blurPass(fboA.texture(), fboB, 1.0f);
        blurPass(fboB.texture(), fboC, 0.0f);
        ycctex = fboC.texture();
    }

    QOpenGLFramebufferObject & finalFbo = (ycctex == fboB.texture()) ? fboA : fboB;
    beginPass(finalFbo);
    inverseProgram.bind();
    inverseProgram.setUniformValue("src", 0);
    inverseProgram.setUniformValue("invCr", 1);
    inverseProgram.setUniformValue("invCb", 2);
    gl->glActiveTexture(GL_TEXTURE0); gl->glBindTexture(GL_TEXTURE_2D, ycctex);
    invCr->bind(1); invCb->bind(2);
    drawQuad(inverseProgram);
    inverseProgram.release();

    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    gl->glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_SHORT, readback.data());
    finalFbo.release();

    const uint16_t * px = reinterpret_cast<const uint16_t *>(readback.constData());
    for (int i = 0; i < width * height; ++i)
    {
        img[i * 3 + 0] = px[i * 4 + 0];
        img[i * 3 + 1] = px[i * 4 + 1];
        img[i * 3 + 2] = px[i * 4 + 2];
    }

    delete srcTexture; delete fwdR; delete fwdG; delete fwdB; delete invCr; delete invCb;
    context.doneCurrent();
    if ( reason ) reason->clear();
    return true;
}

namespace
{
QByteArray sharpenShaderSource()
{
    /* Fixed 5-tap cross sharpen (raw_processing.c:1911-1915):
     * sharp = ka[center] - ky[up] - ky[down] - kx[left] - kx[right], clamped.
     * sharpLut holds (ka, kx, ky) per value. First/last column pass through; rows
     * clamp to edge. */
    return QByteArrayLiteral(
        "uniform sampler2D src;\n"
        "uniform vec2 texSize;\n"
        "uniform sampler2D sharpLut;\n"
        "varying vec2 vTexCoord;\n"
        "vec3 slut(float v)\n"
        "{\n"
        "    float i = clamp(v, 0.0, 65535.0);\n"
        "    vec2 uv = (vec2(mod(i, 256.0), floor(i / 256.0)) + vec2(0.5)) / 256.0;\n"
        "    return texture2D(sharpLut, uv).rgb;\n"
        "}\n"
        "vec3 fetch(vec2 p)\n"
        "{\n"
        "    return floor(texture2D(src, (p + vec2(0.5)) / texSize).rgb * 65535.0 + 0.5);\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec2 px = floor(vTexCoord * texSize);\n"
        "    vec3 center = fetch(px);\n"
        "    if (px.x < 0.5 || px.x > texSize.x - 1.5)\n"
        "    {\n"
        "        gl_FragColor = vec4(center / 65535.0, 1.0);\n"
        "        return;\n"
        "    }\n"
        "    vec3 left  = fetch(vec2(px.x - 1.0, px.y));\n"
        "    vec3 right = fetch(vec2(px.x + 1.0, px.y));\n"
        "    vec3 up    = fetch(vec2(px.x, max(px.y - 1.0, 0.0)));\n"
        "    vec3 down  = fetch(vec2(px.x, min(px.y + 1.0, texSize.y - 1.0)));\n"
        "    vec3 sharp;\n"
        "    sharp.r = slut(center.r).r - slut(up.r).b - slut(down.r).b - slut(left.r).g - slut(right.r).g;\n"
        "    sharp.g = slut(center.g).r - slut(up.g).b - slut(down.g).b - slut(left.g).g - slut(right.g).g;\n"
        "    sharp.b = slut(center.b).r - slut(up.b).b - slut(down.b).b - slut(left.b).g - slut(right.b).g;\n"
        "    gl_FragColor = vec4(clamp(sharp, 0.0, 65535.0) / 65535.0, 1.0);\n"
        "}\n");
}

/* 256x256 RGBA32F LUT: texel[v] = (ka=(uint32)(v*a), kx=LIMIT16(v*x),
 * ky=LIMIT16(v*y), 0), matching processingSetSharpening. */
QByteArray packSharpenLutRgba32F(double a, double x, double y)
{
    QByteArray packed(256 * 256 * 4 * static_cast<int>(sizeof(float)), Qt::Uninitialized);
    float * dst = reinterpret_cast<float *>(packed.data());
    for (int v = 0; v < 65536; ++v)
    {
        dst[v * 4 + 0] = static_cast<float>(static_cast<uint32_t>(static_cast<double>(v) * a));
        int kxv = static_cast<int>(static_cast<double>(v) * x); kxv = kxv < 0 ? 0 : (kxv > 65535 ? 65535 : kxv);
        int kyv = static_cast<int>(static_cast<double>(v) * y); kyv = kyv < 0 ? 0 : (kyv > 65535 ? 65535 : kyv);
        dst[v * 4 + 1] = static_cast<float>(kxv);
        dst[v * 4 + 2] = static_cast<float>(kyv);
        dst[v * 4 + 3] = 0.0f;
    }
    return packed;
}
}

static bool applySharpenPostPassGpu(uint16_t * img, int width, int height,
                                    double a, double x, double y,
                                    QString * reason, QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool { if ( reason ) *reason = why; return false; };
    if ( !img || width <= 0 || height <= 0 ) return fail(QStringLiteral("sharpen post-pass invalid buffer"));

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * gl = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface, &context, &gl, reason, rendererDescription) )
        return false;

    QOpenGLShaderProgram program;
    if ( !program.addShaderFromSourceCode(QOpenGLShader::Vertex, gpuPreviewProcessingVertexShaderSource())
      || !program.addShaderFromSourceCode(QOpenGLShader::Fragment, sharpenShaderSource())
      || !program.link() )
    {
        const QString log = program.log();
        context.doneCurrent();
        return fail(QStringLiteral("sharpen shader setup failed: %1").arg(log));
    }

    const QByteArray packedFrame = packRgb16Texture(img, width * height);
    QOpenGLTexture * srcTexture = createFrameTexture(width, height);
    srcTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, packedFrame.constData());
    QOpenGLTexture * sharpLut = createChromaLutTexture();
    const QByteArray lutBytes = packSharpenLutRgba32F(a, x, y);
    sharpLut->setData(QOpenGLTexture::RGBA, QOpenGLTexture::Float32, lutBytes.constData());

    QOpenGLFramebufferObjectFormat fmt;
    fmt.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fmt.setTextureTarget(GL_TEXTURE_2D);
    fmt.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fbo(width, height, fmt);
    if ( !fbo.isValid() )
    {
        delete srcTexture; delete sharpLut;
        context.doneCurrent();
        return fail(QStringLiteral("sharpen framebuffer creation failed"));
    }

    fbo.bind();
    gl->glViewport(0, 0, width, height);
    gl->glDisable(GL_DEPTH_TEST); gl->glDisable(GL_BLEND);
    gl->glClearColor(0.0f, 0.0f, 0.0f, 1.0f); gl->glClear(GL_COLOR_BUFFER_BIT);
    program.bind();
    program.setUniformValue("src", 0);
    program.setUniformValue("texSize", QVector2D(static_cast<float>(width), static_cast<float>(height)));
    program.setUniformValue("sharpLut", 1);
    srcTexture->bind(0);
    sharpLut->bind(1);
    const int posLoc = program.attributeLocation("position");
    const int texLoc = program.attributeLocation("texCoord");
    program.enableAttributeArray(posLoc);
    program.enableAttributeArray(texLoc);
    program.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
    program.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
    gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    gl->glFinish();
    program.disableAttributeArray(posLoc);
    program.disableAttributeArray(texLoc);

    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    gl->glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_SHORT, readback.data());
    fbo.release();

    const uint16_t * px = reinterpret_cast<const uint16_t *>(readback.constData());
    for (int i = 0; i < width * height; ++i)
    {
        img[i * 3 + 0] = px[i * 4 + 0];
        img[i * 3 + 1] = px[i * 4 + 1];
        img[i * 3 + 2] = px[i * 4 + 2];
    }

    program.release();
    delete srcTexture; delete sharpLut;
    context.doneCurrent();
    if ( reason ) reason->clear();
    return true;
}

namespace
{
QByteArray medianShaderSource()
{
    /* Per-pixel square-window median (the middle order statistic, found by counting
     * so any sort order yields the same value) blended with the original by
     * strength (denoiser_2d_median.c). Window up to 5x5 (25 samples). Border pixels
     * (within window/2 of an edge) pass through. */
    return QByteArrayLiteral(
        "uniform sampler2D src;\n"
        "uniform vec2 texSize;\n"
        "uniform float windowF;\n"
        "uniform float edgeF;\n"
        "uniform float middleF;\n"
        "uniform float strengthF;\n"
        "varying vec2 vTexCoord;\n"
        "void main()\n"
        "{\n"
        "    vec2 px = floor(vTexCoord * texSize);\n"
        "    vec3 center = floor(texture2D(src, (px + vec2(0.5)) / texSize).rgb * 65535.0 + 0.5);\n"
        "    if (px.x < edgeF - 0.5 || px.x > texSize.x - edgeF - 0.5 ||\n"
        "        px.y < edgeF - 0.5 || px.y > texSize.y - edgeF - 0.5)\n"
        "    {\n"
        "        gl_FragColor = vec4(center / 65535.0, 1.0);\n"
        "        return;\n"
        "    }\n"
        "    float wr[25]; float wg[25]; float wb[25];\n"
        "    int n = 0;\n"
        "    for (int fx = 0; fx < 5; fx++)\n"
        "    {\n"
        "        if (float(fx) >= windowF) break;\n"
        "        for (int fy = 0; fy < 5; fy++)\n"
        "        {\n"
        "            if (float(fy) >= windowF) break;\n"
        "            vec2 wpx = vec2(px.x + float(fx) - edgeF, px.y + float(fy) - edgeF);\n"
        "            vec3 s = floor(texture2D(src, (wpx + vec2(0.5)) / texSize).rgb * 65535.0 + 0.5);\n"
        "            wr[n] = s.r; wg[n] = s.g; wb[n] = s.b;\n"
        "            n++;\n"
        "        }\n"
        "    }\n"
        "    int mid = int(middleF + 0.5);\n"
        "    vec3 med = vec3(0.0);\n"
        "    for (int a = 0; a < 25; a++)\n"
        "    {\n"
        "        if (a >= n) break;\n"
        "        float cand = wr[a];\n"
        "        int less = 0; int leq = 0;\n"
        "        for (int b = 0; b < 25; b++)\n"
        "        {\n"
        "            if (b >= n) break;\n"
        "            if (wr[b] < cand) less++;\n"
        "            if (wr[b] <= cand) leq++;\n"
        "        }\n"
        "        if (less <= mid && mid < leq) { med.r = cand; break; }\n"
        "    }\n"
        "    for (int a = 0; a < 25; a++)\n"
        "    {\n"
        "        if (a >= n) break;\n"
        "        float cand = wg[a];\n"
        "        int less = 0; int leq = 0;\n"
        "        for (int b = 0; b < 25; b++)\n"
        "        {\n"
        "            if (b >= n) break;\n"
        "            if (wg[b] < cand) less++;\n"
        "            if (wg[b] <= cand) leq++;\n"
        "        }\n"
        "        if (less <= mid && mid < leq) { med.g = cand; break; }\n"
        "    }\n"
        "    for (int a = 0; a < 25; a++)\n"
        "    {\n"
        "        if (a >= n) break;\n"
        "        float cand = wb[a];\n"
        "        int less = 0; int leq = 0;\n"
        "        for (int b = 0; b < 25; b++)\n"
        "        {\n"
        "            if (b >= n) break;\n"
        "            if (wb[b] < cand) less++;\n"
        "            if (wb[b] <= cand) leq++;\n"
        "        }\n"
        "        if (less <= mid && mid < leq) { med.b = cand; break; }\n"
        "    }\n"
        "    vec3 outv = floor(strengthF * med + (1.0 - strengthF) * center);\n"
        "    gl_FragColor = vec4(clamp(outv, 0.0, 65535.0) / 65535.0, 1.0);\n"
        "}\n");
}
}

static bool applyMedianPostPassGpu(uint16_t * img, int width, int height,
                                   int window, int strength,
                                   QString * reason, QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool { if ( reason ) *reason = why; return false; };
    if ( !img || width <= 0 || height <= 0 ) return fail(QStringLiteral("median post-pass invalid buffer"));
    if ( strength > 100 ) strength = 100;
    const int winSize = window * window;
    const int edge = window / 2;
    const int middle = winSize / 2;
    if ( strength == 0 || window == 0 || width <= edge * 2 || height <= edge * 2 )
    {
        if ( reason ) reason->clear();
        return true;  /* engine no-op cases */
    }

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * gl = nullptr;
    if ( !makePreviewProcessingContextCurrent(&surface, &context, &gl, reason, rendererDescription) )
        return false;

    QOpenGLShaderProgram program;
    if ( !program.addShaderFromSourceCode(QOpenGLShader::Vertex, gpuPreviewProcessingVertexShaderSource())
      || !program.addShaderFromSourceCode(QOpenGLShader::Fragment, medianShaderSource())
      || !program.link() )
    {
        const QString log = program.log();
        context.doneCurrent();
        return fail(QStringLiteral("median shader setup failed: %1").arg(log));
    }

    const QByteArray packedFrame = packRgb16Texture(img, width * height);
    QOpenGLTexture * srcTexture = createFrameTexture(width, height);
    srcTexture->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, packedFrame.constData());

    QOpenGLFramebufferObjectFormat fmt;
    fmt.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fmt.setTextureTarget(GL_TEXTURE_2D);
    fmt.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fbo(width, height, fmt);
    if ( !fbo.isValid() )
    {
        delete srcTexture;
        context.doneCurrent();
        return fail(QStringLiteral("median framebuffer creation failed"));
    }

    fbo.bind();
    gl->glViewport(0, 0, width, height);
    gl->glDisable(GL_DEPTH_TEST); gl->glDisable(GL_BLEND);
    gl->glClearColor(0.0f, 0.0f, 0.0f, 1.0f); gl->glClear(GL_COLOR_BUFFER_BIT);
    program.bind();
    program.setUniformValue("src", 0);
    program.setUniformValue("texSize", QVector2D(static_cast<float>(width), static_cast<float>(height)));
    program.setUniformValue("windowF", static_cast<float>(window));
    program.setUniformValue("edgeF", static_cast<float>(edge));
    program.setUniformValue("middleF", static_cast<float>(middle));
    program.setUniformValue("strengthF", static_cast<float>(strength) / 100.0f);
    srcTexture->bind(0);
    const int posLoc = program.attributeLocation("position");
    const int texLoc = program.attributeLocation("texCoord");
    program.enableAttributeArray(posLoc);
    program.enableAttributeArray(texLoc);
    program.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
    program.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
    gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    gl->glFinish();
    program.disableAttributeArray(posLoc);
    program.disableAttributeArray(texLoc);

    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    gl->glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_SHORT, readback.data());
    fbo.release();

    const uint16_t * px = reinterpret_cast<const uint16_t *>(readback.constData());
    for (int i = 0; i < width * height; ++i)
    {
        img[i * 3 + 0] = px[i * 4 + 0];
        img[i * 3 + 1] = px[i * 4 + 1];
        img[i * 3 + 2] = px[i * 4 + 2];
    }

    program.release();
    delete srcTexture;
    context.doneCurrent();
    if ( reason ) reason->clear();
    return true;
}
