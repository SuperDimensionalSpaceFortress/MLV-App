// CPU-DEBAYERED16-HONOUR-SCALE-1: at preview scale 2/4 on the CPU preview-processing
// route (OutputDebayered16), the phase-3 recon worker reconstructs dual ISO at the
// preview scale (mlvDualIsoPreviewScaleReconPlan / ...Run, as for processed8), the
// render debayers that reduced recon (getMlvRawFrameDebayeredFromReducedReconnedRaw16,
// via renderDebayered16FromSlot) and the playback prep develops it at its size with the
// config reduced to it (playbackPrepReducedSourceConfig). Before this card the worker
// reconstructed the full sensor, the render debayered it at full size and the prep
// box-reduced the result by the scale. RenderFrameThread is not linked into the test
// targets; productionStep() below performs the recon worker's admitted branch and the
// render's dispatch exactly as reconFrameForWorker and drawFrame do. The tests pin:
//  (a) the look gate: over >= 8 frames of the large HQ dual-ISO fixture at x2 and x4 the
//      new frame is within the CPU-DUALISO-AT-PREVIEW-SCALE-1 tolerance of the old one
//      (per-channel mean ratio, R/G and B/G, PSNR, MAE), and a wrong bit shift (the
//      test-only seam) fails that gate;
//  (b) exactness: at x1 the debayered bytes with the feature on equal those with the
//      switch at "0"; at x2/x4 with the switch at "0" they equal today's full route;
//  (c) the production step consumes the reduced recon (reduced dims, no second recon),
//      and a reduced slot whose request was not admitted renders today's full frame;
//  (d) the core entry's refusals and its row fill.
// The tests are Debayered16ReconReuse.HonourScale*: like the rest of that class they
// render the large fixture, so CI's pipeline shard plan runs each alone (its soloPattern).
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../platform/qt/Debayered16ReconReusePolicy.h"
#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../platform/qt/PlaybackPrepCpuRoute.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#define HONOUR_SCALE_TEST_SETENV(name, value) _putenv_s((name), (value))
#define HONOUR_SCALE_TEST_UNSETENV(name) _putenv_s((name), "")
#else
#include <signal.h>
#include <sys/mman.h>
#include <unistd.h>
#define HONOUR_SCALE_TEST_SETENV(name, value) setenv((name), (value), 1)
#define HONOUR_SCALE_TEST_UNSETENV(name) unsetenv((name))
#endif

extern "C" {
#include "../../src/debayer/debayer.h"
int debayerBasicU16ReinitDispatchForTesting(void);
}

namespace {

const char * const kSwitches[] = {
    MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV,
    MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV,
    "MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON",
    "MLVAPP_GPU_PLAYBACK_RECON",
    "MLVAPP_DISABLE_PLAYBACK_PREP_REDUCED",
};

struct EnvGuard
{
    EnvGuard() { clear(); }
    ~EnvGuard()
    {
        clear();
        mlvSetDebayered16ReducedReconBitShiftErrorForTesting(0);
    }
    static void clear()
    {
        for (const char * name : kSwitches) HONOUR_SCALE_TEST_UNSETENV(name);
    }
};

void setHonourScaleSwitch(const char * value)
{
    if (value) HONOUR_SCALE_TEST_SETENV(MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV, value);
    else HONOUR_SCALE_TEST_UNSETENV(MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV);
}

struct WorkerState
{
    WorkerState() { llrpInitWorkerState(&state); }
    ~WorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

const int kCpuCores = 4;
const int kLookFrames = 8;

// The large HQ dual-ISO fixture on the route's debayer (bilinear: Bachelor's CPU legs
// log debayer_engine_mode_last=0), with the exposure match settled by one full render
// as the first full-resolution playback frame settles it in the app.
bool openLargeHq(MlvPipelineFixture & fixture)
{
    QString error;
    if (!fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error))
        return false;
    if (!fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"), &error))
        return false;
    if (!fixture.applyReceipt(&error)) return false;
    setMlvDontAlwaysUseAmaze(fixture.video());
    setMlvCpuCores(fixture.video(), kCpuCores);
    if (fixture.renderFrame8(0).empty()) return false;
    resetMlvCachedFrame(fixture.video());
    return llrpHQDualIso(fixture.video()) != 0;
}

size_t fullPixels(const MlvPipelineFixture & fixture)
{
    return static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height());
}

// The prep inputs buildPlaybackPrepResult passes during zoom-fit playback with nothing
// that refuses a reduction (the owner-shape legs).
PlaybackPrepReducedInputs playbackPrepInputs(int scale, int sourceWidth, int sourceHeight, int mlvWidth, int mlvHeight,
                                             const GpuPreviewProcessingConfig & config)
{
    PlaybackPrepReducedInputs in;
    in.playbackActive = true;
    in.zoomFit = true;
    in.spatialPostPass = gpuPreviewProcessingCpuHasSpatialPostPass(config);
    in.highlightReconExact = config.applyHighlightReconstruction && !config.highlightReconDualIso;
    in.shQuarterBlur = config.applyShadowsHighlights && config.shadowsHighlightsBlurQuarter;
    in.requestedScale = scale;
    in.sourceWidth = sourceWidth;
    in.sourceHeight = sourceHeight;
    in.mlvWidth = mlvWidth;
    in.mlvHeight = mlvHeight;
    return in;
}

// The receipt's preview-processing config, as MainWindow builds it for the CPU route.
GpuPreviewProcessingConfig receiptConfig(MlvPipelineFixture & fixture)
{
    QString reason;
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    if (config.enabled && gpuPreviewProcessingNeedsShadowsHighlightsFrameState(config))
    {
        // The engine's frame state from a full render, attached at the full dims as
        // the render thread attaches it.
        (void)fixture.renderFrame16(1, 1);
        (void)gpuPreviewProcessingAttachFrameState(&config, fixture.processing(), fixture.width(), fixture.height(),
                                                   &reason);
    }
    std::printf("[debayered16-honour-scale] config enabled=%d reason='%s' vignette=%d gradient=%d sh=%d sh_ready=%d "
                "sh_quarter=%d spatial_post=%d highlight_recon=%d\n",
                config.enabled ? 1 : 0, reason.toStdString().c_str(), config.applyVignette ? 1 : 0,
                config.applyGradient ? 1 : 0, config.applyShadowsHighlights ? 1 : 0,
                config.shadowsHighlightsFrameStateReady ? 1 : 0, config.shadowsHighlightsBlurQuarter ? 1 : 0,
                gpuPreviewProcessingCpuHasSpatialPostPass(config) ? 1 : 0,
                config.applyHighlightReconstruction ? 1 : 0);
    return config;
}

// CONTROL arm, today's route: the worker's full recon, the render's debayer of it
// (getMlvRawFrameDebayeredFromReconnedRaw16), at full size.
bool controlDebayered16(MlvPipelineFixture & fixture, uint64_t frame, WorkerState & worker,
                        std::vector<uint16_t> * rgb16)
{
    const size_t n = fullPixels(fixture);
    std::vector<uint16_t> raw(n);
    if (getMlvRawFrameUint16(fixture.video(), frame, raw.data()) != 0) return false;
    applyLLRawProcObjectWorker(fixture.video(), raw.data(), n * sizeof(uint16_t), &worker.state, 0);
    std::vector<float> scratch(n);
    std::copy_n(raw.data(), n, reinterpret_cast<uint16_t *>(scratch.data()));
    rgb16->assign(n * 3u, 0u);
    return getMlvRawFrameDebayeredFromReconnedRaw16(fixture.video(), frame, scratch.data(), rgb16->data(),
                                                    llrpHQDualIso(fixture.video()) ? 1 : 0) == 1;
}

// ...then the prep with its own factor-s decision.
bool controlPrep(MlvPipelineFixture & fixture, const std::vector<uint16_t> & rgb16, int scale,
                 const GpuPreviewProcessingConfig & config, std::vector<uint8_t> * out8, int * w, int * h)
{
    const int fw = fixture.width();
    const int fh = fixture.height();
    const PlaybackPrepReducedDecision decision =
        playbackPrepDecideReduced(playbackPrepInputs(scale, fw, fh, fw, fh, config));
    if (decision.factor != scale)
    {
        std::printf("[debayered16-honour-scale] control prep refused x%d: %s\n", scale,
                    playbackPrepReducedRefusalName(decision.refusal));
        return false;
    }
    PlaybackPrepCpuWorkspace workspace;
    const PlaybackPrepCpuRouteResult route =
        playbackPrepCpuRouteRun(workspace, config, 1, rgb16.data(), fw, fh, decision, out8);
    *w = route.processedWidth;
    *h = route.processedHeight;
    return route.reducedFactor == scale;
}

// The recon worker's admitted branch: the reduced plan and run on the decoded frame.
bool reducedRecon(MlvPipelineFixture & fixture, uint64_t frame, int scale, WorkerState & worker,
                  mlvDualIsoPreviewScaleRecon_t * plan, std::vector<uint16_t> * reduced)
{
    if (!mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, plan) || plan->scale != scale)
    {
        std::printf("[debayered16-honour-scale] frame %d x%d plan refused: %s\n", static_cast<int>(frame), scale,
                    plan->reason);
        return false;
    }
    std::vector<uint16_t> raw(fullPixels(fixture));
    if (getMlvRawFrameUint16(fixture.video(), frame, raw.data()) != 0) return false;
    reduced->assign(static_cast<size_t>(plan->reducedWidth) * static_cast<size_t>(plan->reducedHeight), 0u);
    return mlvDualIsoPreviewScaleReconRun(fixture.video(), plan, raw.data(), reduced->data(), &worker.state,
                                          kCpuCores, nullptr, nullptr) == 1;
}

// HEAD arm: the reduced recon, the render's reduced debayer, then the prep at the reduced
// dims (SourceAlreadyReduced, factor 1) with the config reduced to them.
bool headArm(MlvPipelineFixture & fixture, uint64_t frame, int scale, WorkerState & worker,
             const GpuPreviewProcessingConfig & config, std::vector<uint8_t> * out8, int * w, int * h,
             mlvDualIsoPreviewScaleRecon_t * plan)
{
    std::vector<uint16_t> reduced;
    if (!reducedRecon(fixture, frame, scale, worker, plan, &reduced)) return false;
    const int fw = fixture.width();
    const int fh = fixture.height();
    std::vector<uint16_t> rgb16(fullPixels(fixture) * 3u, 0u);
    int rw = 0, rh = 0;
    if (getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), frame, reduced.data(), plan->reducedWidth,
                                                        plan->reducedHeight, plan->scale, rgb16.data(),
                                                        llrpHQDualIso(fixture.video()) ? 1 : 0, &rw, &rh) != 1)
        return false;
    const PlaybackPrepReducedDecision decision =
        playbackPrepDecideReduced(playbackPrepInputs(scale, rw, rh, fw, fh, config));
    ASSERT_TRUE(decision.refusal == PlaybackPrepReducedRefusal::SourceAlreadyReduced);
    ASSERT_EQ(1, decision.factor);
    PlaybackPrepCpuWorkspace workspace;
    GpuPreviewProcessingConfig reducedConfig;
    ASSERT_TRUE(playbackPrepReducedSourceConfig(workspace, config, 1, decision, rw, rh, fw, fh, &reducedConfig));
    const PlaybackPrepCpuRouteResult route =
        playbackPrepCpuRouteRun(workspace, reducedConfig, 1, rgb16.data(), rw, rh, decision, out8);
    *w = route.processedWidth;
    *h = route.processedHeight;
    return route.reducedFactor == 1 && route.processedWidth == rw && route.processedHeight == rh;
}

struct LookMetric
{
    double ratio[3] = { 0.0, 0.0, 0.0 };
    double rgHead = 0.0, rgControl = 0.0, bgHead = 0.0, bgControl = 0.0;
    double mae = 0.0;
    double psnr = 0.0;
};

// test_cpu_dualiso_preview_scale.cpp's metric over the rows both frames reconstruct.
LookMetric compareLook(const std::vector<uint8_t> & head, const std::vector<uint8_t> & control, int width,
                       int comparedRows)
{
    const size_t compared = static_cast<size_t>(width) * 3u * static_cast<size_t>(comparedRows);
    double sumHead[3] = { 0.0, 0.0, 0.0 };
    double sumControl[3] = { 0.0, 0.0, 0.0 };
    double sumAbs = 0.0;
    double sumSq = 0.0;
    for (size_t i = 0; i < compared; ++i)
    {
        const double r = head[i];
        const double f = control[i];
        sumHead[i % 3] += r;
        sumControl[i % 3] += f;
        sumAbs += std::fabs(r - f);
        sumSq += (r - f) * (r - f);
    }
    LookMetric m;
    m.mae = sumAbs / static_cast<double>(compared);
    const double mse = sumSq / static_cast<double>(compared);
    m.psnr = mse > 0.0 ? 10.0 * std::log10(255.0 * 255.0 / mse) : 99.0;
    for (int c = 0; c < 3; ++c) m.ratio[c] = sumControl[c] > 0.0 ? sumHead[c] / sumControl[c] : 1.0;
    m.rgHead = sumHead[0] / sumHead[1];
    m.rgControl = sumControl[0] / sumControl[1];
    m.bgHead = sumHead[2] / sumHead[1];
    m.bgControl = sumControl[2] / sumControl[1];
    return m;
}

bool withinGate(const LookMetric & m)
{
    for (int c = 0; c < 3; ++c)
        if (std::fabs(m.ratio[c] - 1.0) > 0.03) return false;
    return std::fabs(m.rgHead - m.rgControl) <= 0.03
        && std::fabs(m.bgHead - m.bgControl) <= 0.03
        && m.psnr >= 24.0
        && m.mae <= 6.0;
}

struct GateRun
{
    int frames = 0;
    int framesWithinGate = 0;
};

// Both arms over `frames` frames at each scale; prints every frame.
GateRun runLookGate(const char * label, int frames, const std::vector<int> & scales)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeHq(fixture));
    const GpuPreviewProcessingConfig config = receiptConfig(fixture);
    ASSERT_TRUE(config.enabled);
    const int available = static_cast<int>(getMlvFrames(fixture.video()));
    const int count = std::min(frames, available);
    std::printf("[debayered16-honour-scale] %s: fixture %dx%d frames=%d compared=%d\n", label, fixture.width(),
                fixture.height(), available, count);
    GateRun run;
    WorkerState controlWorker;
    WorkerState headWorker;
    for (int f = 0; f < count; ++f)
    {
        std::vector<uint16_t> control16;
        ASSERT_TRUE(controlDebayered16(fixture, static_cast<uint64_t>(f), controlWorker, &control16));
        for (const int scale : scales)
        {
            std::vector<uint8_t> control8;
            int cw = 0, ch = 0;
            ASSERT_TRUE(controlPrep(fixture, control16, scale, config, &control8, &cw, &ch));
            std::vector<uint8_t> head8;
            int hw = 0, hh = 0;
            mlvDualIsoPreviewScaleRecon_t plan;
            ASSERT_TRUE(headArm(fixture, static_cast<uint64_t>(f), scale, headWorker, config, &head8, &hw, &hh, &plan));
            // Same presented dims as today's prep box-reduce.
            ASSERT_EQ(cw, hw);
            ASSERT_EQ(ch, hh);
            ASSERT_EQ(control8.size(), head8.size());
            const LookMetric m = compareLook(head8, control8, hw, plan.reducedHeight);
            const bool within = withinGate(m);
            std::printf("[debayered16-honour-scale] %s frame %d x%d %dx%d (rows compared %d): mean ratio R=%.4f "
                        "G=%.4f B=%.4f R/G %.4f vs %.4f B/G %.4f vs %.4f MAE=%.3f PSNR=%.2f dB within_gate=%d\n",
                        label, f, scale, hw, hh, plan.reducedHeight, m.ratio[0], m.ratio[1], m.ratio[2], m.rgHead,
                        m.rgControl, m.bgHead, m.bgControl, m.mae, m.psnr, within ? 1 : 0);
            ++run.frames;
            if (within) ++run.framesWithinGate;
        }
    }
    return run;
}

// What reconFrameForWorker and drawFrame do for one OutputDebayered16 playback frame:
// the request admission (with the prep factor MainWindow::drawFrame derives), the
// admitted reduced plan + run or the full llrawproc, the recon-done stamp, then the
// render's dispatch with the slot's reduced recon.
struct ProductionSlot
{
    std::vector<uint16_t> rawImage16;
    std::vector<float> scratch;
    std::vector<uint16_t> reducedReconBayer;
    int reducedReconScale = 1;
    int reducedReconWidth = 0;
    int reducedReconHeight = 0;
    Debayered16ReconProvenance provenance;
};

struct ProductionResult
{
    Debayered16HonourScaleRefusal admission = Debayered16HonourScaleRefusal::NotPlaying;
    Debayered16ReconRefusal outcome = Debayered16ReconRefusal::NotReconned;
    Debayered16RenderedDims dims;
    uint64_t renderLlrawprocRuns = 0;
};

ProductionResult productionStep(MlvPipelineFixture & fixture, uint64_t frame, int scale, WorkerState & worker,
                                ProductionSlot & slot, Debayered16ReconReuseCounters * counters,
                                bool overrideAdmitted = false, bool admittedOverride = false,
                                bool workerLastReconUsedCuda = false)
{
    const size_t n = fullPixels(fixture);
    ProductionResult result;
    Debayered16HonourScaleInputs in;
    in.playbackActive = true;
    in.cpuPreviewRoute = true;
    in.amazeDebayer = doesMlvAlwaysUseAmaze(fixture.video()) != 0;
    in.requestedScale = scale;
    in.prepFactor = (scale == 2 || scale == 4) ? scale : 1;
    result.admission = debayered16HonourScaleRefusal(in);
    const bool admitted = result.admission == Debayered16HonourScaleRefusal::None;

    if (slot.rawImage16.size() < n * 3u) slot.rawImage16.resize(n * 3u);
    slot.provenance.settingsAtDecode = getMlvLlrawprocSettingsFingerprint(fixture.video());
    slot.provenance.acquisitionSucceeded = getMlvRawFrameUint16(fixture.video(), frame, slot.rawImage16.data()) == 0;
    slot.reducedReconScale = 1;
    slot.reducedReconWidth = 0;
    slot.reducedReconHeight = 0;
    if (admitted)
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        if (mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan))
        {
            slot.reducedReconBayer.resize(static_cast<size_t>(plan.reducedWidth) * plan.reducedHeight);
            // GPU-DEVICE-LOSS-SELF-HEAL-1 r5: the worker thread's last recon ran on CUDA (the frames before a loss).
            if (workerLastReconUsedCuda) llrpSetGpuPlaybackReconLastUsedForTesting(1);
            const int rc = mlvDualIsoPreviewScaleReconRun(fixture.video(), &plan, slot.rawImage16.data(),
                                                          slot.reducedReconBayer.data(), &worker.state, kCpuCores,
                                                          nullptr, nullptr);
            if (rc == 1)
            {
                slot.reducedReconScale = plan.scale;
                slot.reducedReconWidth = plan.reducedWidth;
                slot.reducedReconHeight = plan.reducedHeight;
            }
            else if (rc < 0)
            {
                (void)getMlvRawFrameUint16(fixture.video(), frame, slot.rawImage16.data());
            }
        }
    }
    if (slot.reducedReconScale <= 1)
        applyLLRawProcObjectWorker(fixture.video(), slot.rawImage16.data(), n * sizeof(uint16_t), &worker.state, 0);
    slot.provenance.done = debayered16ReconDoneStamp(fixture.video());

    Debayered16ReconReuseInputs reuse;
    reuse.consumeReconnedRaw = true;
    applyDebayered16ReconProvenance(slot.provenance, reuse);
    reuse.playbackActive = true;
    reuse.reducedReconScale = slot.reducedReconScale;
    reuse.honourScaleAdmitted = (overrideAdmitted ? admittedOverride : admitted) && slot.reducedReconScale == scale;
    if (slot.reducedReconScale > 1)
    {
        reuse.reducedReconBayer = slot.reducedReconBayer.data();
        reuse.reducedReconWidth = slot.reducedReconWidth;
        reuse.reducedReconHeight = slot.reducedReconHeight;
    }
    reuse.reconFrameNumber = reuse.renderFrameNumber = static_cast<uint32_t>(frame);
    reuse.reconRequestSerial = reuse.renderRequestSerial = 11;
    reuse.reconBufferComplete = slot.rawImage16.size() >= n * 3u;
    llrpResetDebugRunCount();
    result.outcome = renderDebayered16FromSlot(fixture.video(), static_cast<uint32_t>(frame), reuse,
                                               slot.rawImage16.data(), n, slot.scratch, counters, nullptr, nullptr,
                                               &result.dims);
    result.renderLlrawprocRuns = llrpGetDebugRunCount();
    return result;
}

// Bytes of the debayered-16 frame production presents for frames 0..frames-1 at `scale`.
std::vector<std::vector<uint16_t>> playProduction(int scale, int frames, const char * switchValue,
                                                  std::vector<ProductionResult> * results)
{
    setHonourScaleSwitch(switchValue);
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeHq(fixture));
    WorkerState worker;
    ProductionSlot slot;
    std::vector<std::vector<uint16_t>> out;
    for (int f = 0; f < frames; ++f)
    {
        const ProductionResult r = productionStep(fixture, static_cast<uint64_t>(f), scale, worker, slot, nullptr);
        const size_t words = static_cast<size_t>(r.dims.width) * static_cast<size_t>(r.dims.height) * 3u;
        out.emplace_back(slot.rawImage16.begin(), slot.rawImage16.begin() + static_cast<std::ptrdiff_t>(words));
        if (results) results->push_back(r);
    }
    setHonourScaleSwitch(nullptr);
    return out;
}

// CPU-DUALISO-PREVIEW-SCALE-VERIFY-1: what drawFrame does for one OutputDebayered16 frame
// with Phase 3 Disabled: no recon worker ran, so the slot arrives unreconned. The render
// admits the frame, reconstructs it inline and dispatches as for a worker slot.
struct InlineStep
{
    bool wanted = false;
    Debayered16InlineReducedRecon recon;
    Debayered16HonourScaleRefusal admission = Debayered16HonourScaleRefusal::NotPlaying;
    Debayered16ReconRefusal outcome = Debayered16ReconRefusal::NotReconned;
    Debayered16RenderedDims dims;
    uint64_t renderLlrawprocRuns = 0;
    double llrawprocMsAfterDebayer = -1.0;
};

InlineStep inlineStep(MlvPipelineFixture & fixture, uint64_t frame, int scale, ProductionSlot & slot,
                      Debayered16ReconReuseCounters * counters, bool admittedByRequest = true)
{
    const size_t n = fullPixels(fixture);
    InlineStep step;
    Debayered16HonourScaleInputs in;
    in.playbackActive = true;
    in.cpuPreviewRoute = true;
    in.amazeDebayer = doesMlvAlwaysUseAmaze(fixture.video()) != 0;
    in.requestedScale = scale;
    in.prepFactor = (scale == 2 || scale == 4) ? scale : 1;
    step.admission = debayered16HonourScaleRefusal(in);
    const bool admitted = admittedByRequest && step.admission == Debayered16HonourScaleRefusal::None;
    if (slot.rawImage16.size() < n * 3u) slot.rawImage16.resize(n * 3u);

    Debayered16InlineReducedReconInputs decide;
    decide.phase3Disabled = true;
    decide.honourScaleAdmitted = admitted;
    decide.playbackActive = true;
    decide.scale = scale;
    decide.frameCacheMayServe = mlvRawDebayerCacheMayServeFrame(fixture.video(), frame) != 0;
    step.wanted = debayered16InlineReducedReconWanted(decide);
    if (step.wanted)
    {
        slot.reducedReconScale = 1;
        slot.reducedReconWidth = 0;
        slot.reducedReconHeight = 0;
        step.recon = debayered16RunInlineReducedRecon(fixture.video(), static_cast<uint32_t>(frame), scale, kCpuCores,
                                                      slot.rawImage16.data(), slot.rawImage16.size(),
                                                      slot.reducedReconBayer);
        if (step.recon.reconstructed)
        {
            slot.reducedReconScale = step.recon.scale;
            slot.reducedReconWidth = step.recon.reducedWidth;
            slot.reducedReconHeight = step.recon.reducedHeight;
        }
    }

    Debayered16ReconReuseInputs reuse;
    reuse.playbackActive = true;
    reuse.reducedReconScale = slot.reducedReconScale;
    reuse.honourScaleAdmitted = admitted && slot.reducedReconScale == scale;
    if (slot.reducedReconScale > 1)
    {
        reuse.reducedReconBayer = slot.reducedReconBayer.data();
        reuse.reducedReconWidth = slot.reducedReconWidth;
        reuse.reducedReconHeight = slot.reducedReconHeight;
    }
    reuse.reconBufferComplete = slot.rawImage16.size() >= n * 3u;
    if (step.recon.reconstructed) applyDebayered16InlineReducedRecon(step.recon, static_cast<uint32_t>(frame), 11, reuse);
    llrpResetDebugRunCount();
    step.outcome = renderDebayered16FromSlot(fixture.video(), static_cast<uint32_t>(frame), reuse, slot.rawImage16.data(),
                                             n, slot.scratch, counters, nullptr, nullptr, &step.dims);
    step.renderLlrawprocRuns = llrpGetDebugRunCount();
    step.llrawprocMsAfterDebayer = llrpGetLastTotalMilliseconds();
    return step;
}

void printInlineStep(const char * label, int frame, int scale, const InlineStep & s)
{
    std::printf("[debayered16-inline] %s frame %d x%d: wanted=%d reconstructed=%d decoded=%d admission=%s outcome=%s "
                "dims=%dx%d scale=%d render_llrawproc_runs=%llu recon_llrawproc_ms=%.3f after_debayer_ms=%.3f "
                "fallback='%s'\n",
                label, frame, scale, s.wanted ? 1 : 0, s.recon.reconstructed ? 1 : 0, s.recon.decoded ? 1 : 0,
                debayered16HonourScaleRefusalName(s.admission), debayered16ReconRefusalName(s.outcome), s.dims.width,
                s.dims.height, s.dims.reducedScale, static_cast<unsigned long long>(s.renderLlrawprocRuns),
                s.recon.llrawprocMs, s.llrawprocMsAfterDebayer, s.recon.fallbackReason);
}

std::vector<uint16_t> presentedWords(const ProductionSlot & slot, const Debayered16RenderedDims & dims)
{
    const size_t words = static_cast<size_t>(dims.width) * static_cast<size_t>(dims.height) * 3u;
    return std::vector<uint16_t>(slot.rawImage16.begin(), slot.rawImage16.begin() + static_cast<std::ptrdiff_t>(words));
}

// Histogram exposure matching not yet settled at full resolution: the plan refuses.
void unsettleExposureMatch(MlvPipelineFixture & fixture)
{
    fixture.video()->llrawproc->diso_auto_correction = -2;
    fixture.video()->llrawproc->diso_ev_correction = 1;
    fixture.video()->llrawproc->diso_black_delta = -1;
}

// Today's frame from a fresh object with the same settings.
std::vector<uint16_t> todaysFrame(uint64_t frame, bool unsettled)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeHq(fixture));
    if (unsettled) unsettleExposureMatch(fixture);
    return fixture.renderDebayeredFrame16(frame);
}

} // namespace

// CPU-DUALISO-PREVIEW-SCALE-VERIFY-1: the decision, one case per false branch.
TEST(Debayered16ReconReuse, HonourScaleInlineWantedNamesEveryFalseBranch)
{
    EnvGuard guard;
    const auto wanted = []() {
        Debayered16InlineReducedReconInputs in;
        in.phase3Disabled = true;
        in.honourScaleAdmitted = true;
        in.playbackActive = true;
        in.scale = 4;
        in.frameCacheMayServe = false;
        return in;
    };
    ASSERT_TRUE(debayered16InlineReducedReconWanted(wanted()));
    Debayered16InlineReducedReconInputs in = wanted();
    in.scale = 2;
    ASSERT_TRUE(debayered16InlineReducedReconWanted(in));

    in = wanted();
    in.slotReconned = true;
    ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    in = wanted();
    in.phase3Disabled = false;
    ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    in = wanted();
    in.honourScaleAdmitted = false;
    ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    in = wanted();
    in.playbackActive = false;
    ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    for (const int scale : { 0, 1, 3, 8 })
    {
        in = wanted();
        in.scale = scale;
        ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    }
    in = wanted();
    in.frameCacheMayServe = true;
    ASSERT_FALSE(debayered16InlineReducedReconWanted(in));
    // Default-constructed inputs refuse: every field fails closed.
    ASSERT_FALSE(debayered16InlineReducedReconWanted(Debayered16InlineReducedReconInputs()));
    // The reuse kill switch would refuse the slot anyway: no recon is spent on it.
    HONOUR_SCALE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "1");
    ASSERT_FALSE(debayered16InlineReducedReconWanted(wanted()));
}

// With no worker slot an admitted x2/x4 frame is reconstructed inline and consumed.
TEST(Debayered16ReconReuse, HonourScaleInlineReconConsumedWithNoWorkerSlot)
{
    EnvGuard guard;
    for (const int scale : { 2, 4 })
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openLargeHq(fixture));
        ProductionSlot slot;
        Debayered16ReconReuseCounters counters;
        for (int f = 0; f < 2; ++f)
        {
            const InlineStep s = inlineStep(fixture, static_cast<uint64_t>(f), scale, slot, &counters);
            printInlineStep("no worker slot", f, scale, s);
            ASSERT_TRUE(s.admission == Debayered16HonourScaleRefusal::None);
            ASSERT_TRUE(s.wanted);
            ASSERT_TRUE(s.outcome == Debayered16ReconRefusal::None);
            ASSERT_EQ(scale, s.dims.reducedScale);
            ASSERT_EQ(fixture.width() / scale, s.dims.width);
            ASSERT_EQ(fixture.height() / scale, s.dims.height);
            ASSERT_EQ(static_cast<uint64_t>(0), s.renderLlrawprocRuns);
        }
        ASSERT_EQ(static_cast<uint64_t>(2), counters.consumed());
        ASSERT_EQ(static_cast<uint64_t>(0), counters.ownRecon());
    }
}

// The inline route presents the worker route's frame, word for word.
TEST(Debayered16ReconReuse, HonourScaleInlineBytesEqualTheWorkerRoute)
{
    EnvGuard guard;
    const int frames = 4;
    for (const int scale : { 2, 4 })
    {
        const std::vector<std::vector<uint16_t>> workerRoute = playProduction(scale, frames, nullptr, nullptr);
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openLargeHq(fixture));
        ProductionSlot slot;
        for (int f = 0; f < frames; ++f)
        {
            const InlineStep s = inlineStep(fixture, static_cast<uint64_t>(f), scale, slot, nullptr);
            printInlineStep("worker parity", f, scale, s);
            ASSERT_TRUE(s.outcome == Debayered16ReconRefusal::None);
            const std::vector<uint16_t> got = presentedWords(slot, s.dims);
            ASSERT_EQ(workerRoute[static_cast<size_t>(f)].size(), got.size());
            ASSERT_EQ(0, std::memcmp(workerRoute[static_cast<size_t>(f)].data(), got.data(),
                                     got.size() * sizeof(uint16_t)));
        }
    }
}

// Every refusal leaves today's frame, byte for byte, and a plan refusal costs no decode.
TEST(Debayered16ReconReuse, HonourScaleInlineRefusalsRenderTodaysBytes)
{
    EnvGuard guard;
    const uint64_t frame = 1;
    const std::vector<uint16_t> today = todaysFrame(frame, false);
    const std::vector<uint16_t> todayUnsettled = todaysFrame(frame, true);
    const auto expectFrame = [&](const char * label, const std::vector<uint16_t> & expected, const InlineStep & s,
                                 const ProductionSlot & slot, int scale) {
        printInlineStep(label, static_cast<int>(frame), scale, s);
        ASSERT_TRUE(s.outcome != Debayered16ReconRefusal::None);
        ASSERT_EQ(1, s.dims.reducedScale);
        const std::vector<uint16_t> got = presentedWords(slot, s.dims);
        ASSERT_EQ(expected.size(), got.size());
        ASSERT_EQ(0, std::memcmp(expected.data(), got.data(), got.size() * sizeof(uint16_t)));
    };
    for (const int scale : { 2, 4 })
    {
        {
            setHonourScaleSwitch("0");
            MlvPipelineFixture fixture;
            ASSERT_TRUE(openLargeHq(fixture));
            ProductionSlot slot;
            const InlineStep s = inlineStep(fixture, frame, scale, slot, nullptr);
            setHonourScaleSwitch(nullptr);
            ASSERT_TRUE(s.admission == Debayered16HonourScaleRefusal::KillSwitch);
            ASSERT_FALSE(s.wanted);
            expectFrame("kill switch", today, s, slot, scale);
        }
        {
            MlvPipelineFixture fixture;
            ASSERT_TRUE(openLargeHq(fixture));
            ProductionSlot slot;
            const InlineStep s = inlineStep(fixture, frame, scale, slot, nullptr, false);
            ASSERT_FALSE(s.wanted);
            expectFrame("not admitted", today, s, slot, scale);
        }
        {
            HONOUR_SCALE_TEST_SETENV("MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON", "1");
            MlvPipelineFixture fixture;
            ASSERT_TRUE(openLargeHq(fixture));
            ProductionSlot slot;
            const InlineStep s = inlineStep(fixture, frame, scale, slot, nullptr);
            HONOUR_SCALE_TEST_UNSETENV("MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON");
            ASSERT_TRUE(s.wanted);
            ASSERT_FALSE(s.recon.reconstructed);
            ASSERT_FALSE(s.recon.decoded);
            ASSERT_TRUE(std::strlen(s.recon.fallbackReason) > 0);
            expectFrame("refused plan", today, s, slot, scale);
        }
        {
            MlvPipelineFixture fixture;
            ASSERT_TRUE(openLargeHq(fixture));
            unsettleExposureMatch(fixture);
            ProductionSlot slot;
            const InlineStep s = inlineStep(fixture, frame, scale, slot, nullptr);
            ASSERT_TRUE(s.wanted);
            ASSERT_FALSE(s.recon.reconstructed);
            ASSERT_FALSE(s.recon.decoded);
            ASSERT_EQ(std::string("dual-ISO exposure match not settled at full resolution yet"),
                      std::string(s.recon.fallbackReason));
            expectFrame("unsettled exposure match", todayUnsettled, s, slot, scale);
        }
    }
}

// Export after reduced playback in one process equals a fresh object's export: the inline
// recon publishes nothing the export reads.
TEST(Debayered16ReconReuse, HonourScaleInlinePlaybackLeavesExportUntouched)
{
    EnvGuard guard;
    const uint64_t frame = 1;
    MlvPipelineFixture played;
    ASSERT_TRUE(openLargeHq(played));
    ProductionSlot slot;
    for (const int scale : { 2, 4 })
    {
        for (int f = 0; f < 2; ++f)
        {
            const InlineStep s = inlineStep(played, static_cast<uint64_t>(f), scale, slot, nullptr);
            ASSERT_TRUE(s.outcome == Debayered16ReconRefusal::None);
            ASSERT_EQ(scale, s.dims.reducedScale);
        }
    }
    const std::vector<uint16_t> playedPaused = played.renderDebayeredFrame16(frame);
    const std::vector<uint16_t> playedExport16 = played.renderFrame16(frame);
    const std::vector<uint8_t> playedExport8 = played.renderFrame8(frame);

    MlvPipelineFixture fresh;
    ASSERT_TRUE(openLargeHq(fresh));
    ASSERT_TRUE(playedPaused == fresh.renderDebayeredFrame16(frame));
    ASSERT_TRUE(playedExport16 == fresh.renderFrame16(frame));
    ASSERT_TRUE(playedExport8 == fresh.renderFrame8(frame));
}

// The reduced debayer clears the thread's llrawproc timing, so the recon's own is captured
// first: a head frame reporting 0.0 is a telemetry bug, not a win.
TEST(Debayered16ReconReuse, HonourScaleInlineReconTimingSurvivesTheReducedDebayer)
{
    EnvGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeHq(fixture));
    ProductionSlot slot;
    const InlineStep s = inlineStep(fixture, 1, 4, slot, nullptr);
    printInlineStep("timing", 1, 4, s);
    ASSERT_TRUE(s.outcome == Debayered16ReconRefusal::None);
    ASSERT_TRUE(s.recon.decoded);
    ASSERT_TRUE(s.recon.llrawprocMs > 0.0);
    ASSERT_TRUE(s.llrawprocMsAfterDebayer == 0.0);
}

// Adversary A1: the full-resolution fix pass's telemetry is cleared the same way, so a frame
// with a completed pass must carry it out of the recon or it publishes zero dimensions.
TEST(Debayered16ReconReuse, HonourScaleInlineFullResFixTelemetrySurvivesTheReducedDebayer)
{
    EnvGuard guard;
    for (const int scale : { 2, 4 })
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openLargeHq(fixture));
        fixture.video()->llrawproc->focus_pixels = 1;
        ProductionSlot slot;
        const InlineStep s = inlineStep(fixture, 1, scale, slot, nullptr);
        printInlineStep("full-res fixes", 1, scale, s);
        ASSERT_TRUE(s.outcome == Debayered16ReconRefusal::None);
        ASSERT_TRUE(s.recon.fullResFixesMs > 0.0);
        ASSERT_TRUE(s.recon.preDualIsoFixCompleted);
        ASSERT_TRUE(s.recon.preDualIsoFixMs > 0.0);
        ASSERT_TRUE(llrpGetLastPreDualIsoFixCompleted() == 0);
    }
}

// (a) The look gate at x2 and x4 over 8 frames of the large fixture (16 frames available).
TEST(Debayered16ReconReuse, HonourScaleReducedReconLookWithinGateOfTodaysPrepReducedFrame)
{
    EnvGuard guard;
    const GateRun run = runLookGate("look gate", kLookFrames, { 2, 4 });
    std::printf("[debayered16-honour-scale] look gate: %d of %d frame-scales within the gate\n",
                run.framesWithinGate, run.frames);
    ASSERT_EQ(kLookFrames * 2, run.frames);
    ASSERT_EQ(run.frames, run.framesWithinGate);
}

// (a) Mutation: a wrong bit shift in the reduced debayer (the test-only seam) fails the gate.
TEST(Debayered16ReconReuse, HonourScaleWrongBitShiftFailsTheLookGate)
{
    EnvGuard guard;
    mlvSetDebayered16ReducedReconBitShiftErrorForTesting(1);
    const GateRun run = runLookGate("mutation bit shift +1", 2, { 2, 4 });
    mlvSetDebayered16ReducedReconBitShiftErrorForTesting(0);
    std::printf("[debayered16-honour-scale] mutation: %d of %d frame-scales within the gate\n", run.framesWithinGate,
                run.frames);
    ASSERT_EQ(4, run.frames);
    ASSERT_EQ(0, run.framesWithinGate);
}

// (b) x1 is unchanged: the bytes with the feature on equal the bytes with the switch at
// "0", and the recon worker never reduces.
TEST(Debayered16ReconReuse, HonourScaleScale1BytesUnchangedBySwitch)
{
    EnvGuard guard;
    const int frames = 3;
    std::vector<ProductionResult> onResults;
    std::vector<ProductionResult> offResults;
    const std::vector<std::vector<uint16_t>> on = playProduction(1, frames, nullptr, &onResults);
    const std::vector<std::vector<uint16_t>> off = playProduction(1, frames, "0", &offResults);
    for (int f = 0; f < frames; ++f)
    {
        std::printf("[debayered16-honour-scale] x1 frame %d: on admission=%s outcome=%s words=%zu; off "
                    "admission=%s outcome=%s\n",
                    f, debayered16HonourScaleRefusalName(onResults[f].admission),
                    debayered16ReconRefusalName(onResults[f].outcome), on[f].size(),
                    debayered16HonourScaleRefusalName(offResults[f].admission),
                    debayered16ReconRefusalName(offResults[f].outcome));
        ASSERT_TRUE(onResults[f].admission == Debayered16HonourScaleRefusal::ScaleUnsupported);
        ASSERT_TRUE(offResults[f].admission == Debayered16HonourScaleRefusal::KillSwitch);
        ASSERT_EQ(1, onResults[f].dims.reducedScale);
        ASSERT_EQ(on[f].size(), off[f].size());
        ASSERT_EQ(0, std::memcmp(on[f].data(), off[f].data(), on[f].size() * sizeof(uint16_t)));
    }
}

// (b) With the switch at "0", x2 and x4 render today's full-resolution route byte-for-byte.
TEST(Debayered16ReconReuse, HonourScaleKillSwitchRestoresTodaysFullRouteAtScale2And4)
{
    EnvGuard guard;
    const int frames = 2;
    MlvPipelineFixture reference;
    ASSERT_TRUE(openLargeHq(reference));
    std::vector<std::vector<uint16_t>> expected;
    for (int f = 0; f < frames; ++f) expected.push_back(reference.renderDebayeredFrame16(static_cast<uint64_t>(f)));
    for (const int scale : { 2, 4 })
    {
        std::vector<ProductionResult> results;
        const std::vector<std::vector<uint16_t>> got = playProduction(scale, frames, "0", &results);
        for (int f = 0; f < frames; ++f)
        {
            std::printf("[debayered16-honour-scale] kill switch x%d frame %d: admission=%s outcome=%s dims=%dx%d\n",
                        scale, f, debayered16HonourScaleRefusalName(results[f].admission),
                        debayered16ReconRefusalName(results[f].outcome), results[f].dims.width,
                        results[f].dims.height);
            ASSERT_TRUE(results[f].admission == Debayered16HonourScaleRefusal::KillSwitch);
            ASSERT_EQ(1, results[f].dims.reducedScale);
            ASSERT_EQ(expected[f].size(), got[f].size());
            ASSERT_EQ(0, std::memcmp(expected[f].data(), got[f].data(), got[f].size() * sizeof(uint16_t)));
        }
    }
}

// (c) The production step at x2/x4 consumes the reduced recon: no llrawproc on the render,
// counted as consumed, the reduced dims, and the bytes of the core entry. A reduced slot
// whose request was not admitted is refused as reduced-scale and renders today's full frame
// (its rawImage16 is the decoded frame, never debayered as a recon).
TEST(Debayered16ReconReuse, HonourScaleProductionStepConsumesReducedReconOrRendersFullFrame)
{
    EnvGuard guard;
    MlvPipelineFixture reference;
    ASSERT_TRUE(openLargeHq(reference));
    const std::vector<uint16_t> expectedFull = reference.renderDebayeredFrame16(1);
    for (const int scale : { 2, 4 })
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openLargeHq(fixture));
        WorkerState worker;
        ProductionSlot slot;
        Debayered16ReconReuseCounters counters;
        (void)productionStep(fixture, 0, scale, worker, slot, &counters);
        const ProductionResult r = productionStep(fixture, 1, scale, worker, slot, &counters);
        std::printf("[debayered16-honour-scale] production x%d: admission=%s outcome=%s dims=%dx%d scale=%d "
                    "render_llrawproc_runs=%llu consumed=%llu own_recon=%llu\n",
                    scale, debayered16HonourScaleRefusalName(r.admission), debayered16ReconRefusalName(r.outcome),
                    r.dims.width, r.dims.height, r.dims.reducedScale,
                    static_cast<unsigned long long>(r.renderLlrawprocRuns),
                    static_cast<unsigned long long>(counters.consumed()),
                    static_cast<unsigned long long>(counters.ownRecon()));
        ASSERT_TRUE(r.admission == Debayered16HonourScaleRefusal::None);
        ASSERT_TRUE(r.outcome == Debayered16ReconRefusal::None);
        ASSERT_EQ(scale, r.dims.reducedScale);
        ASSERT_EQ(fixture.width() / scale, r.dims.width);
        ASSERT_EQ(fixture.height() / scale, r.dims.height);
        ASSERT_EQ(static_cast<uint64_t>(0), r.renderLlrawprocRuns);
        ASSERT_EQ(static_cast<uint64_t>(0), counters.ownRecon());
        ASSERT_EQ(static_cast<uint64_t>(2), counters.consumed());

        // Not admitted (overridden after the worker reduced it): today's full frame.
        const ProductionResult refused = productionStep(fixture, 1, scale, worker, slot, &counters, true, false);
        const size_t words = fullPixels(fixture) * 3u;
        std::printf("[debayered16-honour-scale] production x%d not admitted: outcome=%s reason='%s' dims=%dx%d\n",
                    scale, debayered16ReconRefusalName(refused.outcome),
                    debayered16ReconRefusalReason(refused.outcome), refused.dims.width, refused.dims.height);
        ASSERT_TRUE(refused.outcome == Debayered16ReconRefusal::ReducedScale);
        ASSERT_EQ(fixture.width(), refused.dims.width);
        ASSERT_EQ(fixture.height(), refused.dims.height);
        ASSERT_EQ(1, refused.dims.reducedScale);
        ASSERT_EQ(expectedFull.size(), words);
        ASSERT_EQ(0, std::memcmp(expectedFull.data(), slot.rawImage16.data(), words * sizeof(uint16_t)));
    }
}

// GPU-DEVICE-LOSS-SELF-HEAL-1 r5, the declared route (hub ruling r5): a CUDA-preset session after a
// device loss (MLVAPP_GPU_PLAYBACK_RECON=1 kept, CUDA suppressed, and the recon worker thread's last
// recon a CUDA one) presents the native CPU route's frame. The CPU-subset Debayered16 render consumes
// the x4 reduced recon (no gpu_playback_recon refusal, no llrawproc on the render), byte for byte
// the frame the CPU route (no CUDA preset) presents. The no-fault CUDA session keeps its own route:
// its CPU plan refuses ("GPU playback recon requested").
// RED on 79ecc20b (the plan refuses, so the slot is a full-res recon: reducedScale 1) and on
// c5ab7d0b (the stale CUDA stamp: outcome gpu_playback_recon). Both arms run with no full-res
// raw fixes (as M16-1243 at the venue): the fix pass is a full applyLLRawProcObjectWorker, which
// restamps the flag itself and would hide a missing restamp in the reduced recon.
TEST(Debayered16ReconReuse, HonourScaleCudaSuppressedSessionPresentsTheCpuRoutesReducedRecon)
{
    EnvGuard guard;
    const int scale = 4;
    const int frames = 2;
    const auto openWithoutFullResFixes = [](MlvPipelineFixture & fixture) {
        if (!openLargeHq(fixture)) return false;
        fixture.video()->llrawproc->focus_pixels = 0;
        fixture.video()->llrawproc->bad_pixels = 0;
        fixture.video()->llrawproc->vertical_stripes = 0;
        fixture.video()->llrawproc->pattern_noise = 0;
        return true;
    };
    std::vector<std::vector<uint16_t>> cpuRoute;
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openWithoutFullResFixes(fixture));
        mlvDualIsoPreviewScaleRecon_t cpuPlan;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &cpuPlan));
        ASSERT_EQ(0, cpuPlan.fullResFixes);
        WorkerState worker;
        ProductionSlot slot;
        for (int f = 0; f < frames; ++f)
        {
            const ProductionResult r = productionStep(fixture, static_cast<uint64_t>(f), scale, worker, slot, nullptr);
            ASSERT_TRUE(r.outcome == Debayered16ReconRefusal::None);
            ASSERT_EQ(scale, r.dims.reducedScale);
            const size_t words = static_cast<size_t>(r.dims.width) * static_cast<size_t>(r.dims.height) * 3u;
            cpuRoute.emplace_back(slot.rawImage16.begin(), slot.rawImage16.begin() + static_cast<std::ptrdiff_t>(words));
        }
    }

    struct SuppressedScope
    {
        SuppressedScope() { llrpSetGpuCudaSuppressed(1); }
        ~SuppressedScope()
        {
            llrpSetGpuCudaSuppressed(0);
            llrpSetGpuPlaybackReconLastUsedForTesting(0);
        }
    };
    HONOUR_SCALE_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openWithoutFullResFixes(fixture));
    // The GL surface the preset installed stays, presenting the 16-bit frame; the prep reduces that.
    // Presenting the 8-bit image through it instead (r4's F1) is refused (the GUI fixture's prepFactor).
    PlaybackPrepReducedInputs prep = playbackPrepInputs(scale, fixture.width(), fixture.height(), fixture.width(),
                                                        fixture.height(), GpuPreviewProcessingConfig());
    ASSERT_EQ(scale, playbackPrepDecideReduced(prep).factor);
    prep.gpuImagePresentation = true;
    ASSERT_TRUE(playbackPrepDecideReduced(prep).refusal == PlaybackPrepReducedRefusal::GpuImagePresentation);
    mlvDualIsoPreviewScaleRecon_t plan;
    ASSERT_EQ(0, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
    ASSERT_EQ(std::string("GPU playback recon requested"), std::string(plan.reason));
    {
        const SuppressedScope suppressed;
        WorkerState worker;
        ProductionSlot slot;
        Debayered16ReconReuseCounters counters;
        for (int f = 0; f < frames; ++f)
        {
            const ProductionResult r = productionStep(fixture, static_cast<uint64_t>(f), scale, worker, slot, &counters,
                                                      false, false, true);
            std::printf("[debayered16-honour-scale] cuda suppressed x%d frame %d: admission=%s outcome=%s dims=%dx%d "
                        "scale=%d render_llrawproc_runs=%llu\n",
                        scale, f, debayered16HonourScaleRefusalName(r.admission), debayered16ReconRefusalName(r.outcome),
                        r.dims.width, r.dims.height, r.dims.reducedScale,
                        static_cast<unsigned long long>(r.renderLlrawprocRuns));
            ASSERT_TRUE(r.admission == Debayered16HonourScaleRefusal::None);
            ASSERT_EQ(scale, r.dims.reducedScale);
            ASSERT_TRUE(r.outcome == Debayered16ReconRefusal::None);
            ASSERT_EQ(fixture.width() / scale, r.dims.width);
            ASSERT_EQ(fixture.height() / scale, r.dims.height);
            ASSERT_EQ(static_cast<uint64_t>(0), r.renderLlrawprocRuns);
            const size_t words = static_cast<size_t>(r.dims.width) * static_cast<size_t>(r.dims.height) * 3u;
            ASSERT_EQ(cpuRoute[static_cast<size_t>(f)].size(), words);
            ASSERT_EQ(0, std::memcmp(cpuRoute[static_cast<size_t>(f)].data(), slot.rawImage16.data(), words * sizeof(uint16_t)));
        }
        ASSERT_EQ(static_cast<uint64_t>(0), counters.count(Debayered16ReconRefusal::GpuPlaybackRecon));
        ASSERT_EQ(static_cast<uint64_t>(frames), counters.consumed());
    }
}

// (d) The core entry refuses scale 1, scale 8, a wrong width and AMaZE without writing, and
// fills the output to Height/scale rows by repeating the last reconstructed row.
TEST(Debayered16ReconReuse, HonourScaleCoreEntryRefusalsAndRowFill)
{
    EnvGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeHq(fixture));
    WorkerState worker;
    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        std::vector<uint16_t> reduced;
        ASSERT_TRUE(reducedRecon(fixture, 1, scale, worker, &plan, &reduced));
        const int outW = fixture.width() / scale;
        const int outH = fixture.height() / scale;
        const size_t outWords = static_cast<size_t>(outW) * static_cast<size_t>(outH) * 3u;
        const uint16_t sentinel = 0xBEEFu;
        std::vector<uint16_t> out(outWords + 64u, sentinel);
        int w = -1, h = -1;
        const auto untouched = [&]() {
            return std::all_of(out.begin(), out.end(), [&](uint16_t v) { return v == sentinel; }) && w == -1 && h == -1;
        };
        const int hq = llrpHQDualIso(fixture.video()) ? 1 : 0;
        ASSERT_EQ(0, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(), plan.reducedWidth,
                                                                     plan.reducedHeight, 1, out.data(), hq, &w, &h));
        ASSERT_TRUE(untouched());
        ASSERT_EQ(0, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(), plan.reducedWidth,
                                                                     plan.reducedHeight, 8, out.data(), hq, &w, &h));
        ASSERT_TRUE(untouched());
        ASSERT_EQ(0, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(),
                                                                     plan.reducedWidth - 2, plan.reducedHeight, scale,
                                                                     out.data(), hq, &w, &h));
        ASSERT_TRUE(untouched());
        ASSERT_EQ(0, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(), plan.reducedWidth,
                                                                     outH + 1, scale, out.data(), hq, &w, &h));
        ASSERT_TRUE(untouched());
        setMlvAlwaysUseAmaze(fixture.video());
        ASSERT_EQ(0, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(), plan.reducedWidth,
                                                                     plan.reducedHeight, scale, out.data(), hq, &w, &h));
        ASSERT_TRUE(untouched());
        setMlvDontAlwaysUseAmaze(fixture.video());

        ASSERT_EQ(1, getMlvRawFrameDebayeredFromReducedReconnedRaw16(fixture.video(), 1, reduced.data(), plan.reducedWidth,
                                                                     plan.reducedHeight, scale, out.data(), hq, &w, &h));
        ASSERT_EQ(outW, w);
        ASSERT_EQ(outH, h);
        std::printf("[debayered16-honour-scale] core entry x%d: reduced %dx%d out %dx%d filled rows %d\n", scale,
                    plan.reducedWidth, plan.reducedHeight, w, h, outH - plan.reducedHeight);
        ASSERT_TRUE(plan.reducedHeight <= outH);
        const size_t rowWords = static_cast<size_t>(outW) * 3u;
        const uint16_t * lastRow = out.data() + static_cast<size_t>(plan.reducedHeight - 1) * rowWords;
        for (int y = plan.reducedHeight; y < outH; ++y)
            ASSERT_EQ(0, std::memcmp(out.data() + static_cast<size_t>(y) * rowWords, lastRow, rowWords * sizeof(uint16_t)));
        ASSERT_TRUE(std::all_of(out.begin() + static_cast<std::ptrdiff_t>(outWords), out.end(),
                                [&](uint16_t v) { return v == sentinel; }));
        size_t nonzero = 0;
        for (size_t i = 0; i < outWords; ++i)
            if (out[i] != 0) ++nonzero;
        ASSERT_TRUE(nonzero > outWords / 2);
    }
}

// The prep's reduced-source config: only a SourceAlreadyReduced decision at x2/x4 whose
// source is exactly the clip divided by that scale; otherwise nothing is built.
TEST(Debayered16ReconReuse, HonourScalePrepReducedSourceConfigOnlyForAnExactlyReducedSource)
{
    GpuPreviewProcessingConfig config;
    config.enabled = true;
    PlaybackPrepCpuWorkspace workspace;
    GpuPreviewProcessingConfig reduced;
    PlaybackPrepReducedInputs in;
    in.playbackActive = true;
    in.zoomFit = true;
    in.requestedScale = 2;
    in.sourceWidth = 904;
    in.sourceHeight = 1134;
    in.mlvWidth = 1808;
    in.mlvHeight = 2268;
    PlaybackPrepReducedDecision d = playbackPrepDecideReduced(in);
    ASSERT_TRUE(d.refusal == PlaybackPrepReducedRefusal::SourceAlreadyReduced);
    ASSERT_EQ(2, d.achievedScale);
    ASSERT_TRUE(playbackPrepReducedSourceConfig(workspace, config, 1, d, 904, 1134, 1808, 2268, &reduced));
    ASSERT_TRUE(reduced.enabled);
    ASSERT_FALSE(playbackPrepReducedSourceConfig(workspace, config, 1, d, 904, 1133, 1808, 2268, &reduced));
    in.requestedScale = 4;
    in.sourceWidth = 452;
    in.sourceHeight = 567;
    d = playbackPrepDecideReduced(in);
    ASSERT_TRUE(d.refusal == PlaybackPrepReducedRefusal::SourceAlreadyReduced);
    ASSERT_TRUE(playbackPrepReducedSourceConfig(workspace, config, 1, d, 452, 567, 1808, 2268, &reduced));
    // A full-size source the prep reduces itself is not a reduced source.
    in.sourceWidth = 1808;
    in.sourceHeight = 2268;
    d = playbackPrepDecideReduced(in);
    ASSERT_EQ(4, d.factor);
    ASSERT_FALSE(playbackPrepReducedSourceConfig(workspace, config, 1, d, 1808, 2268, 1808, 2268, &reduced));
    ASSERT_FALSE(playbackPrepReducedSourceConfig(workspace, config, 1, d, 1808, 2268, 1808, 2268, nullptr));
}

// r2 B1 (Dot 4fcd6bc7, adversary A2): the policy admits odd reduced widths (4004x2268 at x4 is
// 1001x564) and allocates exactly reducedWidth*reducedHeight words. The bilinear kernels read
// the x+2 column, which at an odd width is past the row and, on the final row pair, one word
// past the frame. The frame below ends exactly at a no-access page; a read of that page is
// recorded by the fault handler, which then opens the page read-only so the kernel finishes.
namespace {

struct GuardPage
{
    char *lo = nullptr;
    char *hi = nullptr;
    long hits = 0;
    const char *firstFault = nullptr;
};

GuardPage g_guardPage;

#ifdef _WIN32
LONG CALLBACK guardPageHandler(EXCEPTION_POINTERS *info)
{
    const EXCEPTION_RECORD *rec = info->ExceptionRecord;
    if (rec->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || rec->NumberParameters < 2)
        return EXCEPTION_CONTINUE_SEARCH;
    const char *addr = reinterpret_cast<const char *>(rec->ExceptionInformation[1]);
    if (addr < g_guardPage.lo || addr >= g_guardPage.hi)
        return EXCEPTION_CONTINUE_SEARCH;
    if (g_guardPage.hits++ == 0)
        g_guardPage.firstFault = addr;
    DWORD old = 0;
    VirtualProtect(g_guardPage.lo, static_cast<SIZE_T>(g_guardPage.hi - g_guardPage.lo), PAGE_READONLY, &old);
    return EXCEPTION_CONTINUE_EXECUTION;
}
#else
struct sigaction g_guardPagePrevious;

void guardPageHandler(int sig, siginfo_t *info, void *)
{
    const char *addr = static_cast<const char *>(info->si_addr);
    if (addr < g_guardPage.lo || addr >= g_guardPage.hi)
    {
        sigaction(sig, &g_guardPagePrevious, nullptr);
        return;
    }
    if (g_guardPage.hits++ == 0)
        g_guardPage.firstFault = addr;
    mprotect(g_guardPage.lo, static_cast<size_t>(g_guardPage.hi - g_guardPage.lo), PROT_READ);
}
#endif

// A frame of `bytes` bytes ending exactly at a no-access page; run() reports the guard reads.
class GuardedFrame
{
public:
    explicit GuardedFrame(size_t bytes) : m_bytes(bytes)
    {
#ifdef _WIN32
        SYSTEM_INFO si;
        GetSystemInfo(&si);
        m_page = si.dwPageSize;
#else
        m_page = static_cast<size_t>(sysconf(_SC_PAGESIZE));
#endif
        const size_t dataPages = (bytes + m_page - 1) / m_page;
        m_mapBytes = (dataPages + 1) * m_page;
#ifdef _WIN32
        m_base = static_cast<char *>(VirtualAlloc(nullptr, m_mapBytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
#else
        void *p = mmap(nullptr, m_mapBytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        m_base = p == MAP_FAILED ? nullptr : static_cast<char *>(p);
#endif
        m_guard = m_base ? m_base + dataPages * m_page : nullptr;
    }
    ~GuardedFrame()
    {
        if (!m_base)
            return;
#ifdef _WIN32
        VirtualFree(m_base, 0, MEM_RELEASE);
#else
        munmap(m_base, m_mapBytes);
#endif
    }
    bool ok() const { return m_base != nullptr; }
    void *data() const { return m_guard - m_bytes; }

    // Arms the guard, runs fn, disarms; returns the number of faulting reads of the guard page
    // and the first one's offset past the frame's end in *firstOffset.
    template <typename Fn> long run(Fn fn, long *firstOffset)
    {
        g_guardPage.lo = m_guard;
        g_guardPage.hi = m_guard + m_page;
        g_guardPage.hits = 0;
        g_guardPage.firstFault = nullptr;
#ifdef _WIN32
        DWORD old = 0;
        VirtualProtect(m_guard, m_page, PAGE_NOACCESS, &old);
        void *handler = AddVectoredExceptionHandler(1, guardPageHandler);
        fn();
        RemoveVectoredExceptionHandler(handler);
        VirtualProtect(m_guard, m_page, PAGE_READWRITE, &old);
#else
        struct sigaction sa;
        std::memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = guardPageHandler;
        sa.sa_flags = SA_SIGINFO;
        sigemptyset(&sa.sa_mask);
        sigaction(SIGSEGV, &sa, &g_guardPagePrevious);
        mprotect(m_guard, m_page, PROT_NONE);
        fn();
        mprotect(m_guard, m_page, PROT_READ | PROT_WRITE);
        sigaction(SIGSEGV, &g_guardPagePrevious, nullptr);
#endif
        *firstOffset = g_guardPage.firstFault ? static_cast<long>(g_guardPage.firstFault - m_guard) : -1;
        return g_guardPage.hits;
    }

private:
    size_t m_bytes = 0;
    size_t m_page = 0;
    size_t m_mapBytes = 0;
    char *m_base = nullptr;
    char *m_guard = nullptr;
};

bool hostRunsDebayerAvx2()
{
#if defined(__GNUC__) && !defined(__clang__) && (defined(__x86_64__) || defined(__i386__))
    // The kernel's own kill switch latches scalar whatever the host supports.
    const char *kill = std::getenv("MLVAPP_DISABLE_AVX2");
    if (kill && kill[0] != '\0' && std::strcmp(kill, "0") != 0)
        return false;
    __builtin_cpu_init();
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#else
    return false;
#endif
}

} // namespace

TEST(Debayered16ReconReuse, ReducedDebayerOddWidthReadsStayInsideTheBayerFrame)
{
    EnvGuard guard;
    struct Case { int width; int height; };
    // 1001x564 is the admitted x4 geometry of a 4004x2268 clip (the AVX2 path's scalar tail
    // reaches x=999); 127x20 ends in a short tail; 17x10 is below the AVX2 width floor.
    const Case cases[] = { { 1001, 564 }, { 127, 20 }, { 17, 10 } };
    const bool avx2Host = hostRunsDebayerAvx2();
    int avx2Runs = 0;
    for (const Case &c : cases)
    {
        const size_t pixels = static_cast<size_t>(c.width) * static_cast<size_t>(c.height);
        std::vector<uint16_t> out(pixels * 3u, 0);
        for (const bool scalar : { true, false })
        {
            if (!scalar && !avx2Host)
                continue;
            HONOUR_SCALE_TEST_SETENV("MLVAPP_DISABLE_AVX2_DEBAYER", scalar ? "1" : "");
            const int avx2 = debayerBasicU16ReinitDispatchForTesting();
            ASSERT_EQ(scalar ? 0 : 1, avx2);
            if (!scalar && c.width >= 18)
                ++avx2Runs;
            GuardedFrame frame(pixels * sizeof(uint16_t));
            ASSERT_TRUE(frame.ok());
            uint16_t *bayer = static_cast<uint16_t *>(frame.data());
            for (size_t i = 0; i < pixels; ++i)
                bayer[i] = static_cast<uint16_t>((i * 37u + 1u) & 0x3FFFu);
            long offset = -1;
            const long hits = frame.run([&] { debayerBasicU16(out.data(), bayer, c.width, c.height, 1, 0); }, &offset);
            std::printf("[debayered16-honour-scale] debayerBasicU16 %s %dx%d: guard reads=%ld first offset=%ld\n",
                        scalar ? "scalar" : "avx2", c.width, c.height, hits, offset);
            ASSERT_EQ(0L, hits);
        }
        // The float sibling (debayerBasic) indexes the same 4x4 neighbourhood.
        GuardedFrame frame(pixels * sizeof(float));
        ASSERT_TRUE(frame.ok());
        float *bayer = static_cast<float *>(frame.data());
        for (size_t i = 0; i < pixels; ++i)
            bayer[i] = static_cast<float>((i * 37u + 1u) & 0x3FFFu);
        long offset = -1;
        const long hits = frame.run([&] { debayerBasic(out.data(), bayer, c.width, c.height, 1); }, &offset);
        std::printf("[debayered16-honour-scale] debayerBasic %dx%d: guard reads=%ld first offset=%ld\n", c.width,
                    c.height, hits, offset);
        ASSERT_EQ(0L, hits);
    }
    HONOUR_SCALE_TEST_UNSETENV("MLVAPP_DISABLE_AVX2_DEBAYER");
    debayerBasicU16ReinitDispatchForTesting();
    std::printf("[debayered16-honour-scale] odd-width guard: avx2 host=%d avx2 runs=%d\n", avx2Host ? 1 : 0, avx2Runs);
    ASSERT_EQ(avx2Host ? 2 : 0, avx2Runs);
}
