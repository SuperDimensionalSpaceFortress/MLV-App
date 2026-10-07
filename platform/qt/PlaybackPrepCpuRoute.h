#ifndef PLAYBACKPREPCPUROUTE_H
#define PLAYBACKPREPCPUROUTE_H

/* CPU-PLAYBACK-PREP-WORKER-BUILD-1: the CPU preview-processing route of the
 * playback prep worker (MainWindow::buildPlaybackPrepResult), factored out so
 * it can be tested without a MainWindow.
 *
 * Tier 1 (byte-identical): a prep-thread-owned 16-bit scratch that is resized
 * only when the frame dims change, and the CPU reference straight to 8 bits
 * (gpuPreviewProcessingApplyCpuReferenceTo8).
 *
 * Tier 2 (look-changing, gated): when the requested playback scale is still
 * owed after what the render thread achieved (OutputDebayered16 always
 * debayers at full size), the 16-bit frame is box-reduced by that residual
 * (2 or 4) with the engine's own RGB16 box, and processed at the reduced dims.
 * The vignette and gradient masks are reduced into a cache keyed by (config
 * generation, dims, factor); the shadows/highlights blur is the quarter blur
 * itself at x4, one bilinear stage of it at x2, or the box-reduced full blur.
 * Every frame that does not qualify takes the full-size path, with a named,
 * counted refusal. */

#include "GpuPreviewProcessing.h"

#include <QByteArray>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <thread>
#include <vector>

enum class PlaybackPrepReducedRefusal : int
{
    None = 0,
    KillSwitch,
    NotPlayback,
    ScaleOne,
    SourceAlreadyReduced,
    ResidualUnsupported,
    ScopesVisible,
    Zebras,
    PipelineCapture,
    GpuImagePresentation,
    NotZoomFit,
    SpatialPostPass,
    HighlightReconExact,
    DimsNotDivisible,
    ShQuarterDimsNotDivisible,
    Count
};

const char * playbackPrepReducedRefusalName(PlaybackPrepReducedRefusal refusal);

/* Environment kill switch: any value but "0" keeps every frame at full size. */
const char * playbackPrepReducedKillSwitchName(void);

struct PlaybackPrepReducedInputs
{
    bool killSwitch = false;
    bool playbackActive = false;
    bool scopesVisible = false;
    bool zebrasEnabled = false;
    bool pipelineCaptureActive = false;
    bool gpuImagePresentation = false;
    bool zoomFit = false;
    bool spatialPostPass = false;
    /* Non-dual-ISO highlight reconstruction keys on the EXACT white-level
     * green of each pixel; a box-averaged block of clipped and unclipped pixels
     * misses it, so the reduced frame keeps a green cast the full-size one
     * reconstructs (it fails the tier-2 tolerance; dual-ISO's window passes). */
    bool highlightReconExact = false;
    bool shQuarterBlur = false;
    int requestedScale = 1;
    /* The 16-bit frame the prep worker received (renderedImageWidth/Height). */
    int sourceWidth = 0;
    int sourceHeight = 0;
    /* The clip's full size (getMlvWidth/Height); 0 = same as the source. */
    int mlvWidth = 0;
    int mlvHeight = 0;
};

struct PlaybackPrepReducedDecision
{
    int factor = 1;
    int achievedScale = 1;
    int residual = 1;
    PlaybackPrepReducedRefusal refusal = PlaybackPrepReducedRefusal::None;
};

/* Pure. residual = requestedScale / achievedScale, where achievedScale is
 * mlvWidth / sourceWidth: a source the render thread already reduced is never
 * reduced again for the part of the scale it already honoured. */
PlaybackPrepReducedDecision playbackPrepDecideReduced(const PlaybackPrepReducedInputs & inputs);

/* Per-frame attribution of the CPU route. */
struct PlaybackPrepCpuRouteResult
{
    int processedWidth = 0;
    int processedHeight = 0;
    int reducedFactor = 1;
    PlaybackPrepReducedRefusal refusal = PlaybackPrepReducedRefusal::None;
    double allocMs = 0.0;
    double reduceMs = 0.0;
    GpuPreviewProcessingCpuSpans spans;
};

/* State the prep thread keeps across frames. Not thread-safe by design: one
 * workspace belongs to one thread (the first thread that touches it); a touch
 * from any other thread is recorded (foreignThreadTouches) for the tests and
 * the session summary. */
class PlaybackPrepCpuWorkspace
{
public:
    PlaybackPrepCpuWorkspace() = default;
    PlaybackPrepCpuWorkspace(const PlaybackPrepCpuWorkspace &) = delete;
    PlaybackPrepCpuWorkspace & operator=(const PlaybackPrepCpuWorkspace &) = delete;

    /* Scratch of exactly width*height*3 words, reallocated (default-initialised,
     * never zero-filled) only when the dims change. */
    uint16_t * scratch16(int width, int height);
    size_t scratch16Words() const { return m_scratch16Words; }
    uint64_t scratch16Allocations() const { return m_scratch16Allocations; }

    uint64_t refusalCount(PlaybackPrepReducedRefusal refusal) const;
    uint64_t reducedFrames() const { return m_reducedFrames; }
    uint64_t fullFrames() const { return m_fullFrames; }
    uint64_t maskCacheBuilds() const { return m_maskCacheBuilds; }
    uint64_t foreignThreadTouches() const { return m_foreignThreadTouches; }

    /* Counts the decision (a refusal by its name, a reduction as reduced). */
    void noteDecision(const PlaybackPrepReducedDecision & decision);

    /* Reduced-path buffers and the mask cache (used by playbackPrepCpuRouteRun). */
    uint16_t * reducedInput(size_t words);
    QByteArray & maskCacheVignette() { return m_maskVignette; }
    std::vector<uint16_t> & maskCacheGradient() { return m_maskGradient; }
    bool maskCacheMatches(uint64_t generation, int width, int height, int factor) const;
    void maskCacheStore(uint64_t generation, int width, int height, int factor);

private:
    void touch();

    std::unique_ptr<uint16_t[]> m_scratch16;
    size_t m_scratch16Words = 0;
    int m_scratch16Width = 0;
    int m_scratch16Height = 0;
    uint64_t m_scratch16Allocations = 0;

    std::unique_ptr<uint16_t[]> m_reducedInput;
    size_t m_reducedInputWords = 0;

    bool m_maskValid = false;
    uint64_t m_maskGeneration = 0;
    int m_maskWidth = 0;
    int m_maskHeight = 0;
    int m_maskFactor = 0;
    QByteArray m_maskVignette;
    std::vector<uint16_t> m_maskGradient;
    uint64_t m_maskCacheBuilds = 0;

    std::array<uint64_t, static_cast<size_t>(PlaybackPrepReducedRefusal::Count)> m_refusals {};
    uint64_t m_reducedFrames = 0;
    uint64_t m_fullFrames = 0;

    bool m_ownerSet = false;
    std::thread::id m_owner;
    uint64_t m_foreignThreadTouches = 0;
};

/* Develops one 16-bit frame to 8 bits on the CPU route. decision.factor 1 is
 * the full-size path (byte-identical to gpuPreviewProcessingApplyCpuReference
 * + >> 8); 2 or 4 processes at (width/f) x (height/f). out8 is resized to the
 * processed dims. configGeneration keys the reduced-mask cache. */
PlaybackPrepCpuRouteResult playbackPrepCpuRouteRun(PlaybackPrepCpuWorkspace & workspace,
                                                   const GpuPreviewProcessingConfig & config,
                                                   uint64_t configGeneration,
                                                   const uint16_t * input16,
                                                   int width,
                                                   int height,
                                                   const PlaybackPrepReducedDecision & decision,
                                                   std::vector<uint8_t> * out8);

/* The reduced config playbackPrepCpuRouteRun processes with (exposed for the
 * tests): masks from the workspace cache, the S/H blur at the reduced dims. */
bool playbackPrepBuildReducedConfig(PlaybackPrepCpuWorkspace & workspace,
                                    const GpuPreviewProcessingConfig & config,
                                    uint64_t configGeneration,
                                    int width,
                                    int height,
                                    int factor,
                                    GpuPreviewProcessingConfig * reduced);

#endif // PLAYBACKPREPCPUROUTE_H
