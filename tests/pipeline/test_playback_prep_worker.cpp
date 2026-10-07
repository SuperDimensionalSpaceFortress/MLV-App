// CPU-PLAYBACK-PREP-WORKER-BUILD-1: the CPU route of the playback prep worker.
//  T1  tier 1 is byte-identical: the parallel post-passes, the fused 8-bit
//      entry and the persistent scratch reproduce the pre-card serial
//      ApplyCpuReference + >> 8 (a verbatim copy of the old post-pass code below
//      is the oracle) on every stage of the matrix, at OMP 1, 2 and max threads,
//      with MLVAPP_PLAYBACK_PREP_TIER1 on and off (default off after D1), and
//      across a dims change of the scratch.
//  T4  tier 2 (reduced-scale processing) stays within the pre-registered
//      tolerance of the box-reduced full-size result on the pointwise stages; the
//      spatial post-pass stages are reported and the policy refuses them.
//  T5  the reduced-scale policy refuses, by name and counted, everything it
//      must, and never reduces a source twice.
//  T6  the reduced-mask cache follows the config generation, and the workspace
//      knows when a second thread touches it.
// No assertion here reads a duration. Results are printed ([PREP-T1]/[PREP-T4]),
// never recorded as golden artifacts.
#include "../common/minitest.h"

#include "mlv_pipeline_fixture.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../platform/qt/PlaybackPrepCpuRoute.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/mlv/video_mlv.h"

#include <QString>

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>
#include <omp.h>

namespace {

// ---------------------------------------------------------------------------
// Oracle: the pre-card serial post-passes and 16->8 conversion, verbatim from
// fork/master 9aae8c68 (GpuPreviewProcessing.cpp cpuBoxBlur / cpuChromaPostPass /
// cpuSharpenPostPass / cpuMedianPostPass, MainWindow.cpp convert_rgb16_to_rgb8).
// ---------------------------------------------------------------------------
void legacyBoxBlur(uint16_t * img, int width, int height, int radius, bool doR, bool doG, bool doB)
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

int legacyClamp16(int v)
{
    return v < 0 ? 0 : (v > 65535 ? 65535 : v);
}

void legacyChroma(uint16_t * img, int width, int height, int radius)
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
        img[i * 3 + 0] = static_cast<uint16_t>(legacyClamp16(Y));
        img[i * 3 + 1] = static_cast<uint16_t>(legacyClamp16(Cb));
        img[i * 3 + 2] = static_cast<uint16_t>(legacyClamp16(Cr));
    }
    if ( radius > 0 )
    {
        legacyBoxBlur(img, width, height, radius, false, true, true);
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
        img[i * 3 + 0] = static_cast<uint16_t>(legacyClamp16(R));
        img[i * 3 + 1] = static_cast<uint16_t>(legacyClamp16(G));
        img[i * 3 + 2] = static_cast<uint16_t>(legacyClamp16(B));
    }
}

void legacySharpen(uint16_t * img, int width, int height, double a, double x, double y)
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
                img[(yy * width + xx) * 3 + c] = static_cast<uint16_t>(legacyClamp16(sharp));
            }
    }
}

void legacyMedian(uint16_t * img, int width, int height, int window, int strength)
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

std::vector<uint8_t> legacyConvert(const std::vector<uint16_t> & source)
{
    std::vector<uint8_t> out(source.size());
    for (size_t i = 0; i < source.size(); ++i) out[i] = static_cast<uint8_t>(source[i] >> 8);
    return out;
}

/* The pre-card ApplyCpuReference: the S/H quarter blur expanded single-threaded,
 * the (unchanged) pointwise pass, then the serial post-passes above. */
std::vector<uint16_t> legacyReference16(const GpuPreviewProcessingConfig & config,
                                        const std::vector<uint16_t> & input,
                                        int width,
                                        int height)
{
    GpuPreviewProcessingConfig pointwise = config;
    if ( config.shadowsHighlightsBlurQuarter )
    {
        pointwise.shadowsHighlightsBlurQuarter = false;
        if ( gpuPreviewProcessingHasShadowsHighlightsFrameState(config, width, height) )
        {
            QByteArray full(static_cast<int>(static_cast<size_t>(width) * height * 3u * sizeof(uint16_t)),
                            Qt::Uninitialized);
            ASSERT_TRUE(processingExpandShadowsHighlightsQuarterBlur(
                reinterpret_cast<const uint16_t *>(config.shadowsHighlightsBlur.constData()),
                width / 4, height / 4,
                reinterpret_cast<uint16_t *>(full.data()), width, height, 1) != 0);
            pointwise.shadowsHighlightsBlur = full;
        }
        else
        {
            pointwise.shadowsHighlightsFrameStateReady = false;
            pointwise.shadowsHighlightsBlur.clear();
        }
    }
    pointwise.applyChroma = false;
    pointwise.applySharpen = false;
    pointwise.applyMedian = false;
    std::vector<uint16_t> out(input.size(), 0);
    gpuPreviewProcessingApplyCpuReference(pointwise, input.data(), out.data(), width, height);
    if ( !config.enabled ) return out;
    if ( config.applyChroma ) legacyChroma(out.data(), width, height, config.chromaBlurRadius);
    if ( config.applySharpen ) legacySharpen(out.data(), width, height, config.sharpenA, config.sharpenX, config.sharpenY);
    if ( config.applyMedian ) legacyMedian(out.data(), width, height, config.medianWindow, config.medianStrength);
    return out;
}

// ---------------------------------------------------------------------------
// Fixture and configs.
// ---------------------------------------------------------------------------
struct OmpThreads
{
    explicit OmpThreads(int n) : previous(omp_get_max_threads()) { omp_set_num_threads(n); }
    ~OmpThreads() { omp_set_num_threads(previous); }
    int previous;
};

/* MLVAPP_PLAYBACK_PREP_TIER1 for one scope (tier 1 is off by default after D1). */
struct Tier1Switch
{
    explicit Tier1Switch(bool on)
        : had(qEnvironmentVariableIsSet(gpuPreviewProcessingTier1SwitchName())),
          previous(qgetenv(gpuPreviewProcessingTier1SwitchName()))
    {
        if (on) qputenv(gpuPreviewProcessingTier1SwitchName(), QByteArray("1"));
        else qunsetenv(gpuPreviewProcessingTier1SwitchName());
    }
    ~Tier1Switch()
    {
        if (had) qputenv(gpuPreviewProcessingTier1SwitchName(), previous);
        else qunsetenv(gpuPreviewProcessingTier1SwitchName());
    }
    bool had;
    QByteArray previous;
};

/* Both dual-ISO fixture clips: tiny (2 frames) and large. */
void openFixture(MlvPipelineFixture & fixture, bool large = false)
{
    QString error_message;
    if (large)
        ASSERT_TRUE(fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error_message));
    else
        ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(large ? QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml")
                                          : QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"),
                                    &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
}

void configureBaseSubset(processingObject_t * processing)
{
    processing->AgX = 0;
    processingDontAllowCreativeAdjustments(processing);
    processing->highlight_reconstruction = 0;
    processing->gradient_enable = 0;
    processing->lut_on = 0;
    processing->filter_on = 0;
    processing->exr_mode = 0;
    processing->denoiserStrength = 0;
    processing->rbfDenoiserLuma = 0;
    processing->rbfDenoiserChroma = 0;
    processing->grainStrength = 0;
    processing->ca_desaturate = 0;
    processing->sharpen = 0.0;
    processing->cs_zone.use_cs = 0;
    processing->cs_zone.chroma_blur_radius = 0;
    processing->clarity = 0.0;
    processing->shadows_highlights.shadows = 0.0;
    processing->shadows_highlights.highlights = 0.0;
    processing->vignette_strength = 0;
    processingSetGamut(processing, GAMUT_Rec709);
}

void installLut(processingObject_t * processing, int dim)
{
    lut_t * lut = processing->lut;
    ASSERT_TRUE(lut != nullptr);
    if (lut->cube) { free(lut->cube); lut->cube = nullptr; }
    lut->dimension = static_cast<uint16_t>(dim);
    lut->is3d = 1;
    lut->intensity = 100;
    for (int i = 0; i < 3; ++i) { lut->domain_min[i] = 0.0f; lut->domain_max[i] = 1.0f; }
    const int entries = dim * dim * dim;
    lut->cube = static_cast<float *>(malloc(static_cast<size_t>(entries) * 3 * sizeof(float)));
    ASSERT_TRUE(lut->cube != nullptr);
    for (int b = 0; b < dim; ++b)
        for (int g = 0; g < dim; ++g)
            for (int r = 0; r < dim; ++r)
            {
                const int e = r + g * dim + b * dim * dim;
                const float rn = static_cast<float>(r) / (dim - 1);
                const float gn = static_cast<float>(g) / (dim - 1);
                const float bn = static_cast<float>(b) / (dim - 1);
                lut->cube[e * 3 + 0] = std::min(1.0f, rn * 0.95f + bn * 0.05f);
                lut->cube[e * 3 + 1] = gn * 0.90f + 0.03f;
                lut->cube[e * 3 + 2] = std::min(1.0f, bn * 1.05f);
            }
    processing->lut_on = 1;
}

/* Crop an RGB16 frame (or blur) to the top-left w x h. */
std::vector<uint16_t> cropRgb16(const uint16_t * src, int srcWidth, int width, int height)
{
    std::vector<uint16_t> out(static_cast<size_t>(width) * height * 3u);
    for (int y = 0; y < height; ++y)
        std::memcpy(out.data() + static_cast<size_t>(y) * width * 3u,
                    src + static_cast<size_t>(y) * srcWidth * 3u,
                    static_cast<size_t>(width) * 3u * sizeof(uint16_t));
    return out;
}

enum class Stage
{
    Pointwise, Vignette, ShFull, ShQuarter, Gradient, Lut, HlRecon, HlReconDualIso,
    ChromaR0, ChromaR2, Sharpen, MedianW3, MedianW5
};

const char * stageName(Stage s)
{
    switch (s)
    {
    case Stage::Pointwise: return "pointwise";
    case Stage::Vignette: return "vignette";
    case Stage::ShFull: return "sh_full";
    case Stage::ShQuarter: return "sh_quarter";
    case Stage::Gradient: return "gradient";
    case Stage::Lut: return "lut";
    case Stage::HlRecon: return "hl_recon";
    case Stage::HlReconDualIso: return "hl_recon_dual_iso";
    case Stage::ChromaR0: return "chroma_r0";
    case Stage::ChromaR2: return "chroma_r2";
    case Stage::Sharpen: return "sharpen";
    case Stage::MedianW3: return "median_w3";
    case Stage::MedianW5: return "median_w5";
    }
    return "?";
}

const Stage kAllStages[] = {
    Stage::Pointwise, Stage::Vignette, Stage::ShFull, Stage::ShQuarter, Stage::Gradient,
    Stage::Lut, Stage::HlRecon, Stage::HlReconDualIso, Stage::ChromaR0, Stage::ChromaR2,
    Stage::Sharpen, Stage::MedianW3, Stage::MedianW5
};
const Stage kPointwiseStages[] = {
    Stage::Pointwise, Stage::Vignette, Stage::ShFull, Stage::ShQuarter, Stage::Gradient,
    Stage::Lut, Stage::HlReconDualIso
};
/* Stages the tier-2 policy refuses: the spatial post-passes, and non-dual-ISO
 * highlight reconstruction (exact white-level equality; fails T4 at x2/x4). */
const Stage kRefusedStages[] = {
    Stage::ChromaR0, Stage::ChromaR2, Stage::Sharpen, Stage::MedianW3, Stage::MedianW5,
    Stage::HlRecon
};

/* A frame, cropped to dims divisible by 4 (and, when maxWidth/maxHeight are set,
 * to at most that size: T1's byte identity does not depend on the content, and
 * the hosted CI caps a pipeline shard at 240 s), and the config of one stage of
 * the matrix built for exactly those dims. The tolerance tests use the whole frame. The S/H blur is the engine's own
 * frame state (full res), cropped; the quarter variant is its 4x box. */
struct StageCase
{
    int width = 0;
    int height = 0;
    std::vector<std::vector<uint16_t>> frames;
    GpuPreviewProcessingConfig config;
};

StageCase buildStageCase(MlvPipelineFixture & fixture, Stage stage, int frameCount,
                         double shadows = 0.32, double highlights = -0.26,
                         int maxWidth = 1 << 20, int maxHeight = 1 << 20)
{
    StageCase sc;
    const int fw = fixture.width();
    const int fh = fixture.height();
    sc.width = std::min(fw, maxWidth) & ~3;
    sc.height = std::min(fh, maxHeight) & ~3;
    ASSERT_TRUE(sc.width >= 16 && sc.height >= 16);
    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    configureBaseSubset(p);
    const uint16_t w = static_cast<uint16_t>(sc.width);
    const uint16_t h = static_cast<uint16_t>(sc.height);
    switch (stage)
    {
    case Stage::Vignette:
        processingSetVignetteMask(p, w, h, 0.5f, 0.2f, 1.0f, 1.4f);
        processingSetVignetteStrength(p, 60);
        break;
    case Stage::ShFull:
    case Stage::ShQuarter:
        processingAllowCreativeAdjustments(p);
        processingSetShadows(p, shadows);
        processingSetHighlights(p, highlights);
        break;
    case Stage::Gradient:
        processingSetGradientEnable(p, 1);
        processingSetGradientMask(p, w, h, 0.0f, 0.0f, static_cast<float>(w), static_cast<float>(h));
        processingSetGradientExposure(p, 1.0);
        break;
    case Stage::Lut:
        installLut(p, 17);
        break;
    case Stage::HlRecon:
        p->highlight_reconstruction = 1;
        if (p->dual_iso) *p->dual_iso = 0;
        break;
    case Stage::HlReconDualIso:
        ASSERT_TRUE(p->dual_iso != nullptr);
        p->highlight_reconstruction = 1;
        *p->dual_iso = 1;
        p->highest_green_diso = 30000;
        break;
    case Stage::ChromaR0:
        processingEnableChromaSeparation(p);
        processingSetChromaBlurRadius(p, 0);
        break;
    case Stage::ChromaR2:
        processingEnableChromaSeparation(p);
        processingSetChromaBlurRadius(p, 2);
        break;
    case Stage::Sharpen:
        processingSetSharpening(p, 0.5);
        break;
    case Stage::MedianW3:
        p->denoiserWindow = 3;
        p->denoiserStrength = 80;
        break;
    case Stage::MedianW5:
        p->denoiserWindow = 5;
        p->denoiserStrength = 80;
        break;
    case Stage::Pointwise:
        break;
    }
    QString reason;
    if (!gpuPreviewProcessingIsSupported(p, &reason))
    {
        ::minitest::fail(__FILE__, __LINE__, std::string("stage supported: ") + stageName(stage),
                         reason.toStdString());
    }
    sc.config = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(sc.config.enabled);
    if (stage == Stage::ShFull || stage == Stage::ShQuarter)
    {
        ASSERT_TRUE(!fixture.renderFrame16(0, 1).empty());
        ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&sc.config, p, fw, fh, &reason));
        ASSERT_TRUE(sc.config.shadowsHighlightsFrameStateReady);
        std::vector<uint16_t> blurFull;
        if (sc.config.shadowsHighlightsBlurQuarter)
        {
            /* The engine already held the quarter state: expand it for the crop. */
            std::vector<uint16_t> expanded(static_cast<size_t>(fw) * fh * 3u);
            ASSERT_TRUE(processingExpandShadowsHighlightsQuarterBlur(
                reinterpret_cast<const uint16_t *>(sc.config.shadowsHighlightsBlur.constData()),
                fw / 4, fh / 4, expanded.data(), fw, fh, 1) != 0);
            blurFull = expanded;
        }
        else
        {
            blurFull.assign(reinterpret_cast<const uint16_t *>(sc.config.shadowsHighlightsBlur.constData()),
                            reinterpret_cast<const uint16_t *>(sc.config.shadowsHighlightsBlur.constData())
                                + static_cast<size_t>(fw) * fh * 3u);
        }
        const std::vector<uint16_t> cropped = cropRgb16(blurFull.data(), fw, sc.width, sc.height);
        sc.config.shadowsHighlightsFrameWidth = sc.width;
        sc.config.shadowsHighlightsFrameHeight = sc.height;
        if (stage == Stage::ShQuarter)
        {
            std::vector<uint16_t> quarter(static_cast<size_t>(sc.width / 4) * (sc.height / 4) * 3u);
            ASSERT_TRUE(processingRgbU16BoxDownsample(cropped.data(), quarter.data(),
                                                      sc.width, sc.height, 4, 1) != 0);
            sc.config.shadowsHighlightsBlurQuarter = true;
            sc.config.shadowsHighlightsBlur = QByteArray(reinterpret_cast<const char *>(quarter.data()),
                                                         static_cast<int>(quarter.size() * sizeof(uint16_t)));
        }
        else
        {
            sc.config.shadowsHighlightsBlurQuarter = false;
            sc.config.shadowsHighlightsBlur = QByteArray(reinterpret_cast<const char *>(cropped.data()),
                                                         static_cast<int>(cropped.size() * sizeof(uint16_t)));
        }
        ASSERT_TRUE(gpuPreviewProcessingHasShadowsHighlightsFrameState(sc.config, sc.width, sc.height));
    }
    const int frames = static_cast<int>(std::min<uint64_t>(getMlvFrames(fixture.video()),
                                                          static_cast<uint64_t>(frameCount)));
    ASSERT_TRUE(frames >= 1);
    for (int f = 0; f < frames; ++f)
    {
        const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(static_cast<uint64_t>(f));
        ASSERT_TRUE(!debayered.empty());
        sc.frames.push_back(cropRgb16(debayered.data(), fw, sc.width, sc.height));
    }
    return sc;
}

bool bytesEqual(const void * a, const void * b, size_t bytes)
{
    return std::memcmp(a, b, bytes) == 0;
}

std::vector<int> threadCounts()
{
    std::vector<int> counts = { 1, 2, std::max(2, omp_get_num_procs()) };
    return counts;
}

PlaybackPrepReducedDecision fullDecision()
{
    PlaybackPrepReducedDecision d;
    d.factor = 1;
    d.refusal = PlaybackPrepReducedRefusal::ScaleOne;
    return d;
}

PlaybackPrepReducedDecision reducedDecision(int factor)
{
    PlaybackPrepReducedDecision d;
    d.factor = factor;
    d.residual = factor;
    return d;
}

} // namespace

// ---------------------------------------------------------------------------
// T1
// ---------------------------------------------------------------------------
namespace {
/* Every stage of the matrix on the first two frames of one dual-ISO fixture clip,
 * at OMP 1, 2 and max threads (max only with the switch off), with
 * MLVAPP_PLAYBACK_PREP_TIER1 on or off. The
 * TESTs below cover both clips with tier 1 on (four dual-ISO fixture frames over
 * two frame sizes) and the tiny clip with it off. */
void runTier1ByteIdentity(bool large, bool tier1)
{
    Tier1Switch tier1Scope(tier1);
    int casesChecked = 0;
    int framesChecked = 0;
    {
    MlvPipelineFixture fixture;
    openFixture(fixture, large);
    for (Stage stage : kAllStages)
    {
        const StageCase sc = buildStageCase(fixture, stage, 2, 0.32, -0.26, 512, 384);
        if (stage == Stage::Pointwise) framesChecked += static_cast<int>(sc.frames.size());
        for (size_t f = 0; f < sc.frames.size(); ++f)
        {
            const std::vector<uint16_t> & frame = sc.frames[f];
            std::vector<uint16_t> oracle16;
            {
                OmpThreads serial(1);
                oracle16 = legacyReference16(sc.config, frame, sc.width, sc.height);
            }
            const std::vector<uint8_t> oracle8 = legacyConvert(oracle16);
            /* Switch off: the post-passes and the S/H expansion are serial, so
             * one (max) thread count covers it and keeps the CI shard budget. */
            const std::vector<int> counts =
                tier1 ? threadCounts() : std::vector<int>{ std::max(2, omp_get_num_procs()) };
            for (int threads : counts)
            {
                OmpThreads scope(threads);
                const std::string label = std::string(stageName(stage)) + ".frame" + std::to_string(f)
                    + ".omp" + std::to_string(threads);
                std::vector<uint16_t> new16(frame.size(), 0);
                gpuPreviewProcessingApplyCpuReference(sc.config, frame.data(), new16.data(),
                                                      sc.width, sc.height);
                if (!bytesEqual(new16.data(), oracle16.data(), new16.size() * sizeof(uint16_t)))
                    ::minitest::fail(__FILE__, __LINE__, "16-bit CPU reference == pre-card serial reference", label);

                PlaybackPrepCpuWorkspace workspace;
                std::vector<uint8_t> new8;
                const PlaybackPrepCpuRouteResult route =
                    playbackPrepCpuRouteRun(workspace, sc.config, 1, frame.data(), sc.width, sc.height,
                                            fullDecision(), &new8);
                ASSERT_EQ(1, route.reducedFactor);
                ASSERT_EQ(sc.width, route.processedWidth);
                ASSERT_EQ(oracle8.size(), new8.size());
                if (!bytesEqual(new8.data(), oracle8.data(), new8.size()))
                    ::minitest::fail(__FILE__, __LINE__, "8-bit route == pre-card reference + >> 8", label);
                ASSERT_EQ(tier1 && !gpuPreviewProcessingCpuHasSpatialPostPass(sc.config),
                          route.spans.fused8);
                ++casesChecked;
            }
        }
    }
    }
    ASSERT_EQ(2, framesChecked);
    std::printf("[PREP-T1] %s tier1=%d frames=%d cases=%d\n", large ? "large" : "tiny",
                tier1 ? 1 : 0, framesChecked, casesChecked);
}
} // namespace

TEST(PlaybackPrepWorkerBuild, Tier1ByteIdentityAcrossTheStageMatrixAndThreadCounts)
{
    runTier1ByteIdentity(false, true);
}

TEST(PlaybackPrepWorkerBuild, Tier1ByteIdentityOnTheLargeDualIsoClip)
{
    runTier1ByteIdentity(true, true);
}

TEST(PlaybackPrepWorkerBuild, Tier1ByteIdentityWithTheSwitchOff)
{
    runTier1ByteIdentity(false, false);
}

TEST(PlaybackPrepWorkerBuild, Tier1ScratchFollowsADimsChange)
{
    Tier1Switch tier1Scope(true);
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const StageCase sc = buildStageCase(fixture, Stage::Sharpen, 1);
    const std::vector<uint16_t> & frame = sc.frames[0];
    PlaybackPrepCpuWorkspace workspace;

    /* Small first, then the full crop: the scratch must grow with the dims. */
    const int smallW = sc.width / 2;
    const int smallH = sc.height / 2;
    const std::vector<uint16_t> small = cropRgb16(frame.data(), sc.width, smallW, smallH);
    const struct { const std::vector<uint16_t> * input; int w; int h; } passes[] = {
        { &small, smallW, smallH }, { &frame, sc.width, sc.height }, { &small, smallW, smallH }
    };
    for (const auto & pass : passes)
    {
        std::vector<uint16_t> oracle16;
        {
            OmpThreads serial(1);
            oracle16 = legacyReference16(sc.config, *pass.input, pass.w, pass.h);
        }
        const std::vector<uint8_t> oracle8 = legacyConvert(oracle16);
        /* Size check before the route writes into the scratch, so an un-resized
         * scratch fails here by assertion instead of being overrun. */
        (void)workspace.scratch16(pass.w, pass.h);
        ASSERT_EQ(static_cast<size_t>(pass.w) * pass.h * 3u, workspace.scratch16Words());
        std::vector<uint8_t> out8;
        (void)playbackPrepCpuRouteRun(workspace, sc.config, 1, pass.input->data(), pass.w, pass.h,
                                      fullDecision(), &out8);
        ASSERT_EQ(static_cast<size_t>(pass.w) * pass.h * 3u, workspace.scratch16Words());
        ASSERT_TRUE(bytesEqual(out8.data(), oracle8.data(), out8.size()));
        /* Same dims again: no reallocation. */
        const uint64_t allocations = workspace.scratch16Allocations();
        (void)playbackPrepCpuRouteRun(workspace, sc.config, 1, pass.input->data(), pass.w, pass.h,
                                      fullDecision(), &out8);
        ASSERT_EQ(allocations, workspace.scratch16Allocations());
        ASSERT_TRUE(bytesEqual(out8.data(), oracle8.data(), out8.size()));
    }
    ASSERT_EQ(3u, static_cast<unsigned>(workspace.scratch16Allocations()));
}

TEST(PlaybackPrepWorkerBuild, Tier1FusedEightBitIsTruncationOfTheSixteenBitValue)
{
    /* The fused path must be exactly v >> 8 of the 16-bit value: not rounded,
     * not >> 7. A config whose 16-bit output holds values with low bytes >= 0x80
     * separates all three. */
    Tier1Switch tier1Scope(true);
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const StageCase sc = buildStageCase(fixture, Stage::Pointwise, 1);
    const std::vector<uint16_t> & frame = sc.frames[0];
    std::vector<uint16_t> ref16(frame.size(), 0);
    gpuPreviewProcessingApplyCpuReference(sc.config, frame.data(), ref16.data(), sc.width, sc.height);
    size_t roundingWitnesses = 0;
    for (uint16_t v : ref16) if ((v & 0xFFu) >= 0x80u && v < 0xFF80u) ++roundingWitnesses;
    ASSERT_TRUE(roundingWitnesses > 0);
    std::vector<uint8_t> fused(frame.size(), 0);
    GpuPreviewProcessingCpuSpans spans;
    gpuPreviewProcessingApplyCpuReferenceTo8(sc.config, frame.data(), nullptr, fused.data(),
                                             sc.width, sc.height, &spans);
    ASSERT_TRUE(spans.fused8);
    for (size_t i = 0; i < ref16.size(); ++i)
    {
        if (fused[i] != static_cast<uint8_t>(ref16[i] >> 8))
            ::minitest::fail(__FILE__, __LINE__, "fused 8-bit == v >> 8", std::to_string(i));
    }
}

// ---------------------------------------------------------------------------
// T4
// ---------------------------------------------------------------------------
namespace {

struct ToleranceStats
{
    double meanAbs[3] = { 0, 0, 0 };
    int p999[3] = { 0, 0, 0 };
    int maxAbs[3] = { 0, 0, 0 };
};

ToleranceStats compare8(const std::vector<uint8_t> & c, const std::vector<uint8_t> & r)
{
    ToleranceStats s;
    const size_t pixels = c.size() / 3u;
    for (int ch = 0; ch < 3; ++ch)
    {
        std::vector<int> hist(256, 0);
        double sum = 0.0;
        for (size_t i = 0; i < pixels; ++i)
        {
            const int d = std::abs(static_cast<int>(c[i * 3 + ch]) - static_cast<int>(r[i * 3 + ch]));
            ++hist[d];
            sum += d;
            s.maxAbs[ch] = std::max(s.maxAbs[ch], d);
        }
        s.meanAbs[ch] = pixels ? sum / static_cast<double>(pixels) : 0.0;
        const size_t target = static_cast<size_t>(std::ceil(0.999 * static_cast<double>(pixels)));
        size_t cumulative = 0;
        for (int d = 0; d < 256; ++d)
        {
            cumulative += static_cast<size_t>(hist[d]);
            if (cumulative >= target) { s.p999[ch] = d; break; }
        }
    }
    return s;
}

/* Normalised amplitude (0..1 of full 8-bit scale) of the period-P component of
 * a luma profile: rows averaged into a column profile (axis 0) or columns into
 * a row profile (axis 1). */
double periodEnergy(const std::vector<uint8_t> & img, int width, int height, int period, int axis)
{
    const int n = axis == 0 ? width : height;
    const int m = axis == 0 ? height : width;
    std::complex<double> acc(0.0, 0.0);
    const double pi = 3.14159265358979323846;
    for (int i = 0; i < n; ++i)
    {
        double profile = 0.0;
        for (int j = 0; j < m; ++j)
        {
            const int x = axis == 0 ? i : j;
            const int y = axis == 0 ? j : i;
            const uint8_t * px = img.data() + (static_cast<size_t>(y) * width + x) * 3u;
            profile += 0.25 * px[0] + 0.5 * px[1] + 0.25 * px[2];
        }
        profile /= static_cast<double>(m);
        acc += profile * std::polar(1.0, -2.0 * pi * static_cast<double>(i) / static_cast<double>(period));
    }
    return std::abs(acc) / static_cast<double>(n) / 255.0;
}

double rowPeriod4Energy(const std::vector<uint8_t> & img, int w, int h) { return periodEnergy(img, w, h, 4, 1); }
double colPeriod3Energy(const std::vector<uint8_t> & img, int w, int h) { return periodEnergy(img, w, h, 3, 0); }

std::string statsLine(const ToleranceStats & s, double rowC, double rowR, double colC, double colR)
{
    char buf[512];
    std::snprintf(buf, sizeof buf,
                  "mean=%.3f/%.3f/%.3f p99.9=%d/%d/%d max=%d/%d/%d rowP4 C=%.5f R=%.5f colP3 C=%.5f R=%.5f",
                  s.meanAbs[0], s.meanAbs[1], s.meanAbs[2], s.p999[0], s.p999[1], s.p999[2],
                  s.maxAbs[0], s.maxAbs[1], s.maxAbs[2], rowC, rowR, colC, colR);
    return buf;
}

struct ReducedComparison
{
    ToleranceStats stats;
    double rowC = 0, rowR = 0, colC = 0, colR = 0;
    bool withinTolerance = false;
    std::string line;
};

ReducedComparison compareReduced(const StageCase & sc, const std::vector<uint16_t> & frame, int factor,
                                 PlaybackPrepCpuWorkspace & workspace, uint64_t generation)
{
    ReducedComparison rc;
    std::vector<uint8_t> c8;
    const PlaybackPrepCpuRouteResult route =
        playbackPrepCpuRouteRun(workspace, sc.config, generation, frame.data(), sc.width, sc.height,
                                reducedDecision(factor), &c8);
    ASSERT_EQ(factor, route.reducedFactor);
    const int rw = sc.width / factor;
    const int rh = sc.height / factor;
    ASSERT_EQ(rw, route.processedWidth);
    ASSERT_EQ(rh, route.processedHeight);
    ASSERT_EQ(static_cast<size_t>(rw) * rh * 3u, c8.size());

    std::vector<uint16_t> full16(frame.size(), 0);
    gpuPreviewProcessingApplyCpuReference(sc.config, frame.data(), full16.data(), sc.width, sc.height);
    std::vector<uint16_t> r16(static_cast<size_t>(rw) * rh * 3u, 0);
    ASSERT_TRUE(processingRgbU16BoxDownsample(full16.data(), r16.data(), sc.width, sc.height, factor, 1) != 0);
    const std::vector<uint8_t> r8 = legacyConvert(r16);

    rc.stats = compare8(c8, r8);
    rc.rowC = rowPeriod4Energy(c8, rw, rh);
    rc.rowR = rowPeriod4Energy(r8, rw, rh);
    rc.colC = colPeriod3Energy(c8, rw, rh);
    rc.colR = colPeriod3Energy(r8, rw, rh);
    rc.withinTolerance = true;
    for (int ch = 0; ch < 3; ++ch)
    {
        if (rc.stats.meanAbs[ch] > 1.0 || rc.stats.p999[ch] > 4 || rc.stats.maxAbs[ch] > 16)
            rc.withinTolerance = false;
    }
    if (rc.rowC > 1.10 * rc.rowR + 0.005 || rc.colC > 1.10 * rc.colR + 0.005)
        rc.withinTolerance = false;
    rc.line = statsLine(rc.stats, rc.rowC, rc.rowR, rc.colC, rc.colR);
    return rc;
}

} // namespace

TEST(PlaybackPrepWorkerBuild, Tier2PointwiseStagesStayWithinTheRegisteredTolerance)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    std::string failures;
    for (Stage stage : kPointwiseStages)
    {
        const StageCase sc = buildStageCase(fixture, stage, 3);
        for (int factor : { 2, 4 })
        {
            for (size_t f = 0; f < sc.frames.size(); ++f)
            {
                PlaybackPrepCpuWorkspace workspace;
                const ReducedComparison rc = compareReduced(sc, sc.frames[f], factor, workspace, 7);
                const std::string label = std::string(stageName(stage)) + ".x" + std::to_string(factor)
                    + ".frame" + std::to_string(f);
                std::printf("[PREP-T4] %s %s\n", label.c_str(), rc.line.c_str());
                if (!rc.withinTolerance) failures += label + " ";
            }
        }
    }
    if (!failures.empty())
        ::minitest::fail(__FILE__, __LINE__, "tier-2 tolerance (pointwise)", failures);
}

TEST(PlaybackPrepWorkerBuild, Tier2StrongShadowsHighlightsReadsTheBlurAtTheReducedDims)
{
    /* A strong S/H makes the blur's spatial position matter: a reduced frame
     * that indexed the blur at full-res dims would read the wrong place. */
    MlvPipelineFixture fixture;
    openFixture(fixture);
    for (Stage stage : { Stage::ShFull, Stage::ShQuarter })
    {
        const StageCase sc = buildStageCase(fixture, stage, 1, 0.95, -0.9);
        for (int factor : { 2, 4 })
        {
            PlaybackPrepCpuWorkspace workspace;
            GpuPreviewProcessingConfig reduced;
            ASSERT_TRUE(playbackPrepBuildReducedConfig(workspace, sc.config, 3, sc.width, sc.height,
                                                       factor, &reduced));
            ASSERT_TRUE(!reduced.shadowsHighlightsBlurQuarter);
            ASSERT_EQ(sc.width / factor, reduced.shadowsHighlightsFrameWidth);
            ASSERT_EQ(sc.height / factor, reduced.shadowsHighlightsFrameHeight);
            ASSERT_EQ(static_cast<int>(static_cast<size_t>(sc.width / factor) * (sc.height / factor)
                                       * 3u * sizeof(uint16_t)),
                      reduced.shadowsHighlightsBlur.size());
            const ReducedComparison rc = compareReduced(sc, sc.frames[0], factor, workspace, 3);
            const std::string label = std::string(stageName(stage)) + ".strong.x" + std::to_string(factor);
            std::printf("[PREP-T4] %s %s\n", label.c_str(), rc.line.c_str());
            if (!rc.withinTolerance)
                ::minitest::fail(__FILE__, __LINE__, "tier-2 tolerance (strong S/H) " + label, rc.line);
        }
    }
}

TEST(PlaybackPrepWorkerBuild, Tier2RefusedStagesAreReportedAndRefused)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    for (Stage stage : kRefusedStages)
    {
        const StageCase sc = buildStageCase(fixture, stage, 1);
        for (int factor : { 2, 4 })
        {
            PlaybackPrepCpuWorkspace workspace;
            const ReducedComparison rc = compareReduced(sc, sc.frames[0], factor, workspace, 5);
            const std::string label = std::string(stageName(stage)) + ".x" + std::to_string(factor);
            std::printf("[PREP-T4] %s within=%d %s\n", label.c_str(), rc.withinTolerance ? 1 : 0, rc.line.c_str());
        }
        /* Whatever the numbers, the policy never reduces these configs. */
        PlaybackPrepReducedInputs in;
        in.playbackActive = true;
        in.zoomFit = true;
        in.requestedScale = 4;
        in.sourceWidth = sc.width;
        in.sourceHeight = sc.height;
        in.spatialPostPass = gpuPreviewProcessingCpuHasSpatialPostPass(sc.config);
        in.highlightReconExact = sc.config.applyHighlightReconstruction && !sc.config.highlightReconDualIso;
        ASSERT_TRUE(in.spatialPostPass != in.highlightReconExact);
        const PlaybackPrepReducedDecision d = playbackPrepDecideReduced(in);
        ASSERT_EQ(1, d.factor);
        ASSERT_TRUE(d.refusal == (in.spatialPostPass ? PlaybackPrepReducedRefusal::SpatialPostPass
                                                     : PlaybackPrepReducedRefusal::HighlightReconExact));
    }
}

// ---------------------------------------------------------------------------
// T5
// ---------------------------------------------------------------------------
TEST(PlaybackPrepWorkerBuild, Tier2PolicyRefusesByNameAndCountsEveryRefusal)
{
    PlaybackPrepReducedInputs eligible;
    eligible.playbackActive = true;
    eligible.zoomFit = true;
    eligible.requestedScale = 4;
    eligible.sourceWidth = 1808;
    eligible.sourceHeight = 1016;
    eligible.mlvWidth = 1808;
    eligible.mlvHeight = 1016;
    {
        const PlaybackPrepReducedDecision d = playbackPrepDecideReduced(eligible);
        ASSERT_EQ(4, d.factor);
        ASSERT_TRUE(d.refusal == PlaybackPrepReducedRefusal::None);
    }
    {
        PlaybackPrepReducedInputs in = eligible;
        in.requestedScale = 2;
        ASSERT_EQ(2, playbackPrepDecideReduced(in).factor);
    }
    {
        /* The render thread already honoured x2 of a requested x4: only x2 is owed. */
        PlaybackPrepReducedInputs in = eligible;
        in.sourceWidth = 904;
        in.sourceHeight = 508;
        const PlaybackPrepReducedDecision d = playbackPrepDecideReduced(in);
        ASSERT_EQ(2, d.achievedScale);
        ASSERT_EQ(2, d.residual);
        ASSERT_EQ(2, d.factor);
    }

    struct Case { const char * name; std::function<void(PlaybackPrepReducedInputs &)> mutate; PlaybackPrepReducedRefusal expected; };
    const std::vector<Case> cases = {
        { "kill_switch", [](PlaybackPrepReducedInputs & i) { i.killSwitch = true; }, PlaybackPrepReducedRefusal::KillSwitch },
        { "paused", [](PlaybackPrepReducedInputs & i) { i.playbackActive = false; }, PlaybackPrepReducedRefusal::NotPlayback },
        { "scale_1", [](PlaybackPrepReducedInputs & i) { i.requestedScale = 1; }, PlaybackPrepReducedRefusal::ScaleOne },
        { "residual_1", [](PlaybackPrepReducedInputs & i) { i.requestedScale = 2; i.sourceWidth = 904; i.sourceHeight = 508; },
          PlaybackPrepReducedRefusal::SourceAlreadyReduced },
        { "residual_3", [](PlaybackPrepReducedInputs & i) { i.requestedScale = 3; }, PlaybackPrepReducedRefusal::ResidualUnsupported },
        { "scopes", [](PlaybackPrepReducedInputs & i) { i.scopesVisible = true; }, PlaybackPrepReducedRefusal::ScopesVisible },
        { "zebras", [](PlaybackPrepReducedInputs & i) { i.zebrasEnabled = true; }, PlaybackPrepReducedRefusal::Zebras },
        { "capture", [](PlaybackPrepReducedInputs & i) { i.pipelineCaptureActive = true; }, PlaybackPrepReducedRefusal::PipelineCapture },
        { "gpu_image", [](PlaybackPrepReducedInputs & i) { i.gpuImagePresentation = true; }, PlaybackPrepReducedRefusal::GpuImagePresentation },
        { "not_zoom_fit", [](PlaybackPrepReducedInputs & i) { i.zoomFit = false; }, PlaybackPrepReducedRefusal::NotZoomFit },
        { "spatial", [](PlaybackPrepReducedInputs & i) { i.spatialPostPass = true; }, PlaybackPrepReducedRefusal::SpatialPostPass },
        { "hl_recon_exact", [](PlaybackPrepReducedInputs & i) { i.highlightReconExact = true; }, PlaybackPrepReducedRefusal::HighlightReconExact },
        { "dims", [](PlaybackPrepReducedInputs & i) { i.sourceWidth = 1810; i.mlvWidth = 1810; }, PlaybackPrepReducedRefusal::DimsNotDivisible },
        { "sh_quarter_dims", [](PlaybackPrepReducedInputs & i) { i.requestedScale = 2; i.sourceWidth = 1810; i.mlvWidth = 1810; i.shQuarterBlur = true; },
          PlaybackPrepReducedRefusal::ShQuarterDimsNotDivisible },
    };

    PlaybackPrepCpuWorkspace workspace;
    std::vector<uint16_t> tiny(static_cast<size_t>(8) * 8 * 3u, 1000);
    GpuPreviewProcessingConfig disabled;
    std::vector<uint8_t> out8;
    for (const Case & c : cases)
    {
        PlaybackPrepReducedInputs in = eligible;
        c.mutate(in);
        const PlaybackPrepReducedDecision d = playbackPrepDecideReduced(in);
        if (d.factor != 1 || d.refusal != c.expected)
            ::minitest::fail(__FILE__, __LINE__, std::string("refusal ") + c.name,
                             playbackPrepReducedRefusalName(d.refusal));
        const uint64_t before = workspace.refusalCount(c.expected);
        const PlaybackPrepCpuRouteResult route =
            playbackPrepCpuRouteRun(workspace, disabled, 1, tiny.data(), 8, 8, d, &out8);
        ASSERT_EQ(1, route.reducedFactor);
        ASSERT_TRUE(route.refusal == c.expected);
        ASSERT_EQ(before + 1, workspace.refusalCount(c.expected));
    }
    ASSERT_EQ(static_cast<uint64_t>(cases.size()), workspace.fullFrames());
    ASSERT_EQ(0u, static_cast<unsigned>(workspace.reducedFrames()));

    const PlaybackPrepCpuRouteResult reducedRoute =
        playbackPrepCpuRouteRun(workspace, disabled, 1, tiny.data(), 8, 8,
                                playbackPrepDecideReduced([&] { PlaybackPrepReducedInputs i = eligible; i.sourceWidth = 8; i.sourceHeight = 8; i.mlvWidth = 8; i.mlvHeight = 8; return i; }()),
                                &out8);
    ASSERT_EQ(4, reducedRoute.reducedFactor);
    ASSERT_EQ(1u, static_cast<unsigned>(workspace.reducedFrames()));
    ASSERT_EQ(static_cast<size_t>(2 * 2 * 3), out8.size());
}

// ---------------------------------------------------------------------------
// T6
// ---------------------------------------------------------------------------
TEST(PlaybackPrepWorkerBuild, Tier2MaskCacheFollowsTheConfigGeneration)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    StageCase sc = buildStageCase(fixture, Stage::Gradient, 1);
    const std::vector<uint16_t> & frame = sc.frames[0];
    ASSERT_TRUE(sc.config.gradientMaskData != nullptr);
    const size_t maskCount = static_cast<size_t>(sc.width) * sc.height;
    std::vector<uint16_t> mask(sc.config.gradientMaskData, sc.config.gradientMaskData + maskCount);
    sc.config.gradientMaskData = mask.data();

    PlaybackPrepCpuWorkspace workspace;
    std::vector<uint8_t> first;
    (void)playbackPrepCpuRouteRun(workspace, sc.config, 10, frame.data(), sc.width, sc.height,
                                  reducedDecision(4), &first);
    ASSERT_EQ(1u, static_cast<unsigned>(workspace.maskCacheBuilds()));

    /* Same generation: the cache is reused (the engine bumps the generation on
     * every config change, so an unchanged generation means unchanged masks). */
    std::vector<uint8_t> again;
    (void)playbackPrepCpuRouteRun(workspace, sc.config, 10, frame.data(), sc.width, sc.height,
                                  reducedDecision(4), &again);
    ASSERT_EQ(1u, static_cast<unsigned>(workspace.maskCacheBuilds()));
    ASSERT_TRUE(first == again);

    /* The mask changes behind the same pointer and the generation is bumped: the
     * cache must be rebuilt, and the output must equal a fresh workspace's. */
    for (size_t i = 0; i < maskCount; ++i) mask[i] = static_cast<uint16_t>(65535 - mask[i]);
    std::vector<uint8_t> bumped;
    (void)playbackPrepCpuRouteRun(workspace, sc.config, 11, frame.data(), sc.width, sc.height,
                                  reducedDecision(4), &bumped);
    ASSERT_EQ(2u, static_cast<unsigned>(workspace.maskCacheBuilds()));
    PlaybackPrepCpuWorkspace fresh;
    std::vector<uint8_t> freshOut;
    (void)playbackPrepCpuRouteRun(fresh, sc.config, 11, frame.data(), sc.width, sc.height,
                                  reducedDecision(4), &freshOut);
    ASSERT_TRUE(bumped == freshOut);
    ASSERT_TRUE(bumped != first);

    /* A dims or factor change also rebuilds. */
    std::vector<uint8_t> x2;
    (void)playbackPrepCpuRouteRun(workspace, sc.config, 11, frame.data(), sc.width, sc.height,
                                  reducedDecision(2), &x2);
    ASSERT_EQ(3u, static_cast<unsigned>(workspace.maskCacheBuilds()));
}

TEST(PlaybackPrepWorkerBuild, Tier2DifferentVignetteMasksGiveDifferentOutputs)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const StageCase a = buildStageCase(fixture, Stage::Vignette, 1);
    processingSetVignetteMask(fixture.processing(), static_cast<uint16_t>(a.width),
                              static_cast<uint16_t>(a.height), 0.3f, 0.6f, 1.6f, 0.8f);
    processingSetVignetteStrength(fixture.processing(), -70);
    QString reason;
    GpuPreviewProcessingConfig b = gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(b.enabled && b.applyVignette);
    ASSERT_TRUE(a.config.vignetteMask != b.vignetteMask);

    PlaybackPrepCpuWorkspace workspace;
    std::vector<uint8_t> outA;
    std::vector<uint8_t> outB;
    (void)playbackPrepCpuRouteRun(workspace, a.config, 20, a.frames[0].data(), a.width, a.height,
                                  reducedDecision(4), &outA);
    (void)playbackPrepCpuRouteRun(workspace, b, 21, a.frames[0].data(), a.width, a.height,
                                  reducedDecision(4), &outB);
    ASSERT_TRUE(outA != outB);
    PlaybackPrepCpuWorkspace fresh;
    std::vector<uint8_t> freshB;
    (void)playbackPrepCpuRouteRun(fresh, b, 21, a.frames[0].data(), a.width, a.height,
                                  reducedDecision(4), &freshB);
    ASSERT_TRUE(outB == freshB);
}

TEST(PlaybackPrepWorkerBuild, WorkspaceRecordsATouchFromASecondThread)
{
    PlaybackPrepCpuWorkspace workspace;
    (void)workspace.scratch16(4, 4);
    ASSERT_EQ(0u, static_cast<unsigned>(workspace.foreignThreadTouches()));
    (void)workspace.scratch16(4, 4);
    ASSERT_EQ(0u, static_cast<unsigned>(workspace.foreignThreadTouches()));
    std::thread other([&workspace] { (void)workspace.scratch16(4, 4); });
    other.join();
    ASSERT_EQ(1u, static_cast<unsigned>(workspace.foreignThreadTouches()));
}
