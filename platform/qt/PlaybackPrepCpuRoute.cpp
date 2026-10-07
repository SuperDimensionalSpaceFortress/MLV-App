#include "PlaybackPrepCpuRoute.h"

#include "../../src/processing/raw_processing.h"

#include <QElapsedTimer>

#include <algorithm>
#include <cstring>
#include <omp.h>

namespace
{
double spanMs(const QElapsedTimer & clock, qint64 startNs)
{
    return static_cast<double>(clock.nsecsElapsed() - startNs) / 1000000.0;
}
}

const char * playbackPrepReducedRefusalName(PlaybackPrepReducedRefusal refusal)
{
    switch ( refusal )
    {
    case PlaybackPrepReducedRefusal::None:                      return "none";
    case PlaybackPrepReducedRefusal::KillSwitch:                return "kill_switch";
    case PlaybackPrepReducedRefusal::NotPlayback:               return "not_playback";
    case PlaybackPrepReducedRefusal::ScaleOne:                  return "scale_1";
    case PlaybackPrepReducedRefusal::SourceAlreadyReduced:      return "source_already_reduced";
    case PlaybackPrepReducedRefusal::ResidualUnsupported:       return "residual_unsupported";
    case PlaybackPrepReducedRefusal::ScopesVisible:             return "scopes_visible";
    case PlaybackPrepReducedRefusal::Zebras:                    return "zebras";
    case PlaybackPrepReducedRefusal::PipelineCapture:           return "pipeline_capture";
    case PlaybackPrepReducedRefusal::GpuImagePresentation:      return "gpu_image_presentation";
    case PlaybackPrepReducedRefusal::NotZoomFit:                return "not_zoom_fit";
    case PlaybackPrepReducedRefusal::SpatialPostPass:           return "spatial_post_pass";
    case PlaybackPrepReducedRefusal::HighlightReconExact:       return "highlight_recon_exact";
    case PlaybackPrepReducedRefusal::DimsNotDivisible:          return "dims_not_divisible";
    case PlaybackPrepReducedRefusal::ShQuarterDimsNotDivisible: return "sh_quarter_dims_not_divisible";
    case PlaybackPrepReducedRefusal::Count:                     break;
    }
    return "unknown";
}

const char * playbackPrepReducedKillSwitchName(void)
{
    return "MLVAPP_DISABLE_PLAYBACK_PREP_REDUCED";
}

PlaybackPrepReducedDecision playbackPrepDecideReduced(const PlaybackPrepReducedInputs & in)
{
    PlaybackPrepReducedDecision d;
    auto refuse = [&d](PlaybackPrepReducedRefusal why) -> PlaybackPrepReducedDecision
    {
        d.factor = 1;
        d.refusal = why;
        return d;
    };
    const int mlvWidth = in.mlvWidth > 0 ? in.mlvWidth : in.sourceWidth;
    d.achievedScale = (in.sourceWidth > 0 && mlvWidth > in.sourceWidth)
        ? std::max(1, mlvWidth / in.sourceWidth)
        : 1;
    const int requested = std::max(1, in.requestedScale);
    d.residual = (requested % d.achievedScale == 0) ? requested / d.achievedScale : 0;

    if ( in.killSwitch ) return refuse(PlaybackPrepReducedRefusal::KillSwitch);
    if ( !in.playbackActive ) return refuse(PlaybackPrepReducedRefusal::NotPlayback);
    if ( requested <= 1 ) return refuse(PlaybackPrepReducedRefusal::ScaleOne);
    if ( d.achievedScale > 1 && d.residual <= 1 )
        return refuse(PlaybackPrepReducedRefusal::SourceAlreadyReduced);
    if ( d.residual != 2 && d.residual != 4 )
        return refuse(PlaybackPrepReducedRefusal::ResidualUnsupported);
    if ( in.scopesVisible ) return refuse(PlaybackPrepReducedRefusal::ScopesVisible);
    if ( in.zebrasEnabled ) return refuse(PlaybackPrepReducedRefusal::Zebras);
    if ( in.pipelineCaptureActive ) return refuse(PlaybackPrepReducedRefusal::PipelineCapture);
    if ( in.gpuImagePresentation ) return refuse(PlaybackPrepReducedRefusal::GpuImagePresentation);
    if ( !in.zoomFit ) return refuse(PlaybackPrepReducedRefusal::NotZoomFit);
    if ( in.spatialPostPass ) return refuse(PlaybackPrepReducedRefusal::SpatialPostPass);
    if ( in.highlightReconExact ) return refuse(PlaybackPrepReducedRefusal::HighlightReconExact);
    if ( in.sourceWidth <= 0 || in.sourceHeight <= 0
      || in.sourceWidth % d.residual != 0 || in.sourceHeight % d.residual != 0 )
        return refuse(PlaybackPrepReducedRefusal::DimsNotDivisible);
    if ( in.shQuarterBlur && ( in.sourceWidth % 4 != 0 || in.sourceHeight % 4 != 0 ) )
        return refuse(PlaybackPrepReducedRefusal::ShQuarterDimsNotDivisible);
    d.factor = d.residual;
    d.refusal = PlaybackPrepReducedRefusal::None;
    return d;
}

void PlaybackPrepCpuWorkspace::touch()
{
    const std::thread::id self = std::this_thread::get_id();
    if ( !m_ownerSet )
    {
        m_owner = self;
        m_ownerSet = true;
    }
    else if ( m_owner != self )
    {
        ++m_foreignThreadTouches;
    }
}

uint16_t * PlaybackPrepCpuWorkspace::scratch16(int width, int height)
{
    touch();
    if ( width <= 0 || height <= 0 ) return nullptr;
    if ( !m_scratch16 || width != m_scratch16Width || height != m_scratch16Height )
    {
        const size_t words = static_cast<size_t>(width) * static_cast<size_t>(height) * 3u;
        m_scratch16.reset(new uint16_t[words]);
        m_scratch16Words = words;
        m_scratch16Width = width;
        m_scratch16Height = height;
        ++m_scratch16Allocations;
    }
    return m_scratch16.get();
}

uint16_t * PlaybackPrepCpuWorkspace::reducedInput(size_t words)
{
    touch();
    if ( words == 0 ) return nullptr;
    if ( !m_reducedInput || words != m_reducedInputWords )
    {
        m_reducedInput.reset(new uint16_t[words]);
        m_reducedInputWords = words;
    }
    return m_reducedInput.get();
}

uint64_t PlaybackPrepCpuWorkspace::refusalCount(PlaybackPrepReducedRefusal refusal) const
{
    const size_t index = static_cast<size_t>(refusal);
    return index < m_refusals.size() ? m_refusals[index] : 0;
}

void PlaybackPrepCpuWorkspace::noteDecision(const PlaybackPrepReducedDecision & decision)
{
    touch();
    if ( decision.factor > 1 )
    {
        ++m_reducedFrames;
        return;
    }
    ++m_fullFrames;
    const size_t index = static_cast<size_t>(decision.refusal);
    if ( index < m_refusals.size() ) ++m_refusals[index];
}

bool PlaybackPrepCpuWorkspace::maskCacheMatches(uint64_t generation, int width, int height, int factor) const
{
    return m_maskValid
        && m_maskGeneration == generation
        && m_maskWidth == width
        && m_maskHeight == height
        && m_maskFactor == factor;
}

void PlaybackPrepCpuWorkspace::maskCacheStore(uint64_t generation, int width, int height, int factor)
{
    touch();
    m_maskValid = true;
    m_maskGeneration = generation;
    m_maskWidth = width;
    m_maskHeight = height;
    m_maskFactor = factor;
    ++m_maskCacheBuilds;
}

namespace
{
/* Pixel i of the full frame reads mask[i + 1] (the engine's vmpix pre-increment).
 * Reduced pixel r covers the block of full pixels j; it reads reduced[r + 1] =
 * the mean of the mask values those pixels read (the final pixel's vignette_end
 * skip is not reproduced: one pixel of a smooth mask). */
void reduceVignetteMask(const QByteArray & full, int width, int height, int factor, QByteArray * reduced)
{
    const int rw = width / factor;
    const int rh = height / factor;
    const int count = static_cast<int>(full.size() / static_cast<int>(sizeof(float)));
    const float * src = reinterpret_cast<const float *>(full.constData());
    *reduced = QByteArray(static_cast<int>((static_cast<size_t>(rw) * rh + 1u) * sizeof(float)),
                          Qt::Uninitialized);
    float * dst = reinterpret_cast<float *>(reduced->data());
    dst[0] = count > 0 ? src[0] : 0.0f;
    const float inv = 1.0f / static_cast<float>(factor * factor);
    #pragma omp parallel for schedule(static)
    for (int ry = 0; ry < rh; ++ry)
    {
        for (int rx = 0; rx < rw; ++rx)
        {
            float sum = 0.0f;
            for (int dy = 0; dy < factor; ++dy)
            {
                const int y = ry * factor + dy;
                for (int dx = 0; dx < factor; ++dx)
                {
                    const int x = rx * factor + dx;
                    int k = y * width + x + 1;
                    if ( k >= count ) k = count - 1;
                    sum += (k >= 0) ? src[k] : 0.0f;
                }
            }
            dst[static_cast<size_t>(ry) * rw + rx + 1] = sum * inv;
        }
    }
}

void reduceGradientMask(const uint16_t * full, int width, int height, int factor, std::vector<uint16_t> * reduced)
{
    const int rw = width / factor;
    const int rh = height / factor;
    reduced->resize(static_cast<size_t>(rw) * rh);
    uint16_t * dst = reduced->data();
    const uint32_t area = static_cast<uint32_t>(factor * factor);
    #pragma omp parallel for schedule(static)
    for (int ry = 0; ry < rh; ++ry)
    {
        for (int rx = 0; rx < rw; ++rx)
        {
            uint32_t sum = 0;
            for (int dy = 0; dy < factor; ++dy)
            {
                const uint16_t * row = full + static_cast<size_t>(ry * factor + dy) * width + rx * factor;
                for (int dx = 0; dx < factor; ++dx) sum += row[dx];
            }
            dst[static_cast<size_t>(ry) * rw + rx] = static_cast<uint16_t>(sum / area);
        }
    }
}
}

bool playbackPrepBuildReducedConfig(PlaybackPrepCpuWorkspace & workspace,
                                    const GpuPreviewProcessingConfig & config,
                                    uint64_t configGeneration,
                                    int width,
                                    int height,
                                    int factor,
                                    GpuPreviewProcessingConfig * reduced)
{
    if ( !reduced || (factor != 2 && factor != 4)
      || width <= 0 || height <= 0 || width % factor != 0 || height % factor != 0 )
    {
        return false;
    }
    const int rw = width / factor;
    const int rh = height / factor;
    *reduced = config;

    const bool vignette = config.applyVignette && !config.vignetteMask.isEmpty();
    const bool gradient = config.applyGradient && config.gradientMaskData != nullptr;
    if ( (vignette || gradient)
      && !workspace.maskCacheMatches(configGeneration, width, height, factor) )
    {
        if ( vignette )
            reduceVignetteMask(config.vignetteMask, width, height, factor, &workspace.maskCacheVignette());
        else
            workspace.maskCacheVignette().clear();
        if ( gradient )
            reduceGradientMask(config.gradientMaskData, width, height, factor, &workspace.maskCacheGradient());
        else
            workspace.maskCacheGradient().clear();
        workspace.maskCacheStore(configGeneration, width, height, factor);
    }
    if ( vignette ) reduced->vignetteMask = workspace.maskCacheVignette();
    if ( gradient )
    {
        reduced->gradientMaskData = workspace.maskCacheGradient().empty()
            ? nullptr : workspace.maskCacheGradient().data();
    }

    if ( gpuPreviewProcessingNeedsShadowsHighlightsFrameState(config) )
    {
        const int threads = omp_get_max_threads();
        reduced->shadowsHighlightsBlurQuarter = false;
        reduced->shadowsHighlightsFrameWidth = rw;
        reduced->shadowsHighlightsFrameHeight = rh;
        bool ok = gpuPreviewProcessingHasShadowsHighlightsFrameState(config, width, height);
        if ( ok && config.shadowsHighlightsBlurQuarter )
        {
            if ( factor == 4 )
            {
                /* The quarter blur IS the blur at the x4 dims: no expansion. */
                reduced->shadowsHighlightsBlur = config.shadowsHighlightsBlur;
            }
            else
            {
                QByteArray half(static_cast<int>(static_cast<size_t>(rw) * rh * 3u * sizeof(uint16_t)),
                                Qt::Uninitialized);
                ok = processingRgbU16Upsample2xBilinear(
                         reinterpret_cast<const uint16_t *>(config.shadowsHighlightsBlur.constData()),
                         width / 4, height / 4,
                         reinterpret_cast<uint16_t *>(half.data()), rw, rh, threads) != 0;
                reduced->shadowsHighlightsBlur = half;
            }
        }
        else if ( ok )
        {
            QByteArray blur(static_cast<int>(static_cast<size_t>(rw) * rh * 3u * sizeof(uint16_t)),
                            Qt::Uninitialized);
            ok = processingRgbU16BoxDownsample(
                     reinterpret_cast<const uint16_t *>(config.shadowsHighlightsBlur.constData()),
                     reinterpret_cast<uint16_t *>(blur.data()), width, height, factor, threads) != 0;
            reduced->shadowsHighlightsBlur = blur;
        }
        if ( !ok )
        {
            reduced->shadowsHighlightsFrameStateReady = false;
            reduced->shadowsHighlightsBlur.clear();
        }
    }
    return true;
}

PlaybackPrepCpuRouteResult playbackPrepCpuRouteRun(PlaybackPrepCpuWorkspace & workspace,
                                                   const GpuPreviewProcessingConfig & config,
                                                   uint64_t configGeneration,
                                                   const uint16_t * input16,
                                                   int width,
                                                   int height,
                                                   const PlaybackPrepReducedDecision & decision,
                                                   std::vector<uint8_t> * out8)
{
    PlaybackPrepCpuRouteResult result;
    QElapsedTimer clock;
    clock.start();
    workspace.noteDecision(decision);
    result.refusal = decision.refusal;
    result.processedWidth = width;
    result.processedHeight = height;
    if ( !out8 || width <= 0 || height <= 0 ) return result;

    /* Tier 1 off (the default): every frame gets the pre-card value-initialised
     * 16-bit buffer and the unfused 16-bit pass; tier 1 on: the workspace's
     * persistent scratch, and only when a post-pass needs the 16-bit result. */
    const bool tier1 = gpuPreviewProcessingTier1Enabled();
    const bool scratchNeeded =
        !tier1 || !config.enabled || gpuPreviewProcessingCpuHasSpatialPostPass(config);
    std::vector<uint16_t> frameBuffer16;
    auto scratchFor = [&](int w, int h) -> uint16_t *
    {
        if ( !scratchNeeded ) return nullptr;
        if ( tier1 ) return workspace.scratch16(w, h);
        frameBuffer16.assign(static_cast<size_t>(w) * static_cast<size_t>(h) * 3u, 0);
        return frameBuffer16.data();
    };
    const int factor = decision.factor;
    if ( factor > 1 && input16
      && width % factor == 0 && height % factor == 0 )
    {
        const int rw = width / factor;
        const int rh = height / factor;
        qint64 startNs = clock.nsecsElapsed();
        out8->resize(static_cast<size_t>(rw) * rh * 3u);
        uint16_t * reducedInput = workspace.reducedInput(static_cast<size_t>(rw) * rh * 3u);
        uint16_t * scratch = scratchFor(rw, rh);
        result.allocMs = spanMs(clock, startNs);

        startNs = clock.nsecsElapsed();
        GpuPreviewProcessingConfig reducedConfig;
        const bool built =
            processingRgbU16BoxDownsample(input16, reducedInput, width, height, factor,
                                          omp_get_max_threads()) != 0
            && playbackPrepBuildReducedConfig(workspace, config, configGeneration,
                                              width, height, factor, &reducedConfig);
        result.reduceMs = spanMs(clock, startNs);
        if ( built )
        {
            gpuPreviewProcessingApplyCpuReferenceTo8(reducedConfig, reducedInput, scratch,
                                                     out8->data(), rw, rh, &result.spans);
            result.processedWidth = rw;
            result.processedHeight = rh;
            result.reducedFactor = factor;
            return result;
        }
    }

    qint64 startNs = clock.nsecsElapsed();
    out8->resize(static_cast<size_t>(width) * height * 3u);
    uint16_t * scratch = scratchFor(width, height);
    result.allocMs = spanMs(clock, startNs);
    if ( input16 )
    {
        gpuPreviewProcessingApplyCpuReferenceTo8(config, input16, scratch,
                                                 out8->data(), width, height, &result.spans);
    }
    return result;
}
