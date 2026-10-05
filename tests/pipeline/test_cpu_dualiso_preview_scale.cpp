// CPU-DUALISO-AT-PREVIEW-SCALE-1: the phase-3 recon worker reconstructs dual ISO at
// the playback preview scale (mlvDualIsoPreviewScaleReconPlan / ...Run) and the
// process stage consumes it (getMlvProcessedFrame8ScaledFromReducedReconnedRaw16).
// These tests drive those entries exactly as RenderFrameThread does, on the fixture
// clips, and pin:
//  (a) export/full-quality output is untouched by the new path (same bytes as a
//      clean object; the suite-wide --check-golden hashes cover the rest);
//  (b) playback at x2 and x4 takes the reduced path;
//  (c) the reduced preview is within a stated tolerance of the downscaled full-res
//      recon, including per-channel cast bounds;
//  (d) mutation: forcing full resolution (the kill switch) makes (b)'s check fail;
//  (e) a reduced-scale playback session publishes nothing to the shared llrawproc
//      state, so a later export in the same process equals a fresh-process export;
//  (f) the reduced frame is published under a signature distinct from the full
//      recon, so a paused/scrubbed full-path render is never served it.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/processing/playback_downsample.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#define CPU_DUALISO_TEST_SETENV(name, value) _putenv_s((name), (value))
#define CPU_DUALISO_TEST_UNSETENV(name) _putenv_s((name), "")
#else
#define CPU_DUALISO_TEST_SETENV(name, value) setenv((name), (value), 1)
#define CPU_DUALISO_TEST_UNSETENV(name) unsetenv((name))
#endif

namespace {

const char * const kKillSwitch = "MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON";

// RenderFrameThread's PlaybackPreviewModeGuard, for the duration of one render.
struct PreviewEnvelope
{
    explicit PreviewEnvelope(int scale)
        : previousMode(processingPlaybackPreviewModeEnabled())
        , previousAggressive(processingPlaybackAggressivePreviewModeEnabled())
        , previousScale(processingPlaybackPreviewScaleFactor())
    {
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackAggressivePreviewMode(0);
        processingSetPlaybackPreviewScaleFactor(scale);
    }
    ~PreviewEnvelope()
    {
        processingSetPlaybackPreviewScaleFactor(previousScale);
        processingSetPlaybackAggressivePreviewMode(previousAggressive);
        processingSetPlaybackPreviewMode(previousMode);
    }
    int previousMode;
    int previousAggressive;
    int previousScale;
};

struct WorkerState
{
    WorkerState() { llrpInitWorkerState(&state); }
    ~WorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

struct KillSwitchGuard
{
    KillSwitchGuard() { CPU_DUALISO_TEST_UNSETENV(kKillSwitch); CPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON"); }
    ~KillSwitchGuard() { CPU_DUALISO_TEST_UNSETENV(kKillSwitch); }
};

bool openHqFixture(MlvPipelineFixture & fixture, bool large)
{
    QString error;
    const bool opened = large
        ? fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error)
        : fixture.openTinyDualIso(&error);
    if (!opened) return false;
    if (!fixture.loadReceipt(large ? QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml")
                                   : QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"),
                             &error)) return false;
    return fixture.applyReceipt(&error);
}

std::vector<uint16_t> decodeRaw(MlvPipelineFixture & fixture, uint64_t frame)
{
    std::vector<uint16_t> raw(static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height()));
    const int rc = getMlvRawFrameUint16(fixture.video(), frame, raw.data());
    if (rc != 0)
    {
        std::printf("[cpu-dualiso-preview-scale] frame %d raw decode rc=%d\n", static_cast<int>(frame), rc);
        raw.clear();
    }
    return raw;
}

size_t outputBytes(MlvPipelineFixture & fixture, int scale)
{
    int w = 0, h = 0;
    mlvFrameOutputDimensions(fixture.video(), scale, &w, &h);
    return static_cast<size_t>(w) * static_cast<size_t>(h) * 3u;
}

// What RenderFrameThread does for one frame on the reduced path: recon worker
// (plan + run on a worker state), then the process stage. Returns false whenever
// any step declines, i.e. whenever the frame would have been reconstructed at full
// resolution instead. outPlan/outFrame are filled on success.
bool reducedPathTaken(MlvPipelineFixture & fixture, uint64_t frame, int scale,
                      mlvDualIsoPreviewScaleRecon_t * outPlan, std::vector<uint8_t> * outFrame)
{
    mlvDualIsoPreviewScaleRecon_t plan;
    if (!mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan) || plan.scale != scale)
    {
        std::printf("[cpu-dualiso-preview-scale] frame %d x%d full res: %s\n",
                    static_cast<int>(frame), scale, plan.reason);
        return false;
    }
    std::vector<uint16_t> raw = decodeRaw(fixture, frame);
    if (raw.empty()) return false;
    std::vector<uint16_t> reduced(static_cast<size_t>(plan.reducedWidth) * static_cast<size_t>(plan.reducedHeight));
    WorkerState worker;
    const int rc = mlvDualIsoPreviewScaleReconRun(fixture.video(), &plan, raw.data(), reduced.data(),
                                                  &worker.state, 1, nullptr, nullptr);
    if (rc != 1)
    {
        std::printf("[cpu-dualiso-preview-scale] frame %d x%d run rc=%d\n", static_cast<int>(frame), scale, rc);
        return false;
    }
    std::vector<uint8_t> out(outputBytes(fixture, scale));
    PreviewEnvelope envelope(scale);
    if (getMlvProcessedFrame8ScaledFromReducedReconnedRaw16(fixture.video(), frame, reduced.data(),
                                                            plan.reducedWidth, plan.reducedHeight, plan.scale,
                                                            out.data(), 1, scale) != 1)
    {
        std::printf("[cpu-dualiso-preview-scale] frame %d x%d process stage declined\n",
                    static_cast<int>(frame), scale);
        return false;
    }
    if (outPlan) *outPlan = plan;
    if (outFrame) *outFrame = out;
    return true;
}

// The pre-card phase-3 playback frame: full-sensor recon on a worker state, then
// the reconned-raw process stage (Bayer->RGB box downsample at the preview scale).
bool fullReconPreview(MlvPipelineFixture & fixture, uint64_t frame, int scale, std::vector<uint8_t> * outFrame)
{
    std::vector<uint16_t> raw = decodeRaw(fixture, frame);
    if (raw.empty()) return false;
    WorkerState worker;
    applyLLRawProcObjectWorker(fixture.video(), raw.data(), raw.size() * sizeof(uint16_t), &worker.state, 0);
    std::vector<uint8_t> out(outputBytes(fixture, scale));
    PreviewEnvelope envelope(scale);
    if (getMlvProcessedFrame8ScaledFromReconnedRaw16(fixture.video(), frame, raw.data(), out.data(), 1, scale, 0) != 1)
        return false;
    *outFrame = out;
    return true;
}

struct SharedDualIsoState
{
    int pattern = 0;
    int autoCorrection = 0;
    double evCorrection = 0.0;
    int blackDelta = 0;
};

SharedDualIsoState sharedState(MlvPipelineFixture & fixture)
{
    SharedDualIsoState s;
    s.pattern = fixture.video()->llrawproc->diso_pattern;
    s.autoCorrection = fixture.video()->llrawproc->diso_auto_correction;
    s.evCorrection = fixture.video()->llrawproc->diso_ev_correction;
    s.blackDelta = fixture.video()->llrawproc->diso_black_delta;
    return s;
}

} // namespace

// (b) x2 and x4 playback reconstruct at the preview scale once a full-resolution
// recon has settled the exposure match (the first export-path render does that, as
// the first full-res playback frame does in the app).
TEST(CpuDualIsoPreviewScale, PlaybackAtScale2And4TakesReducedRecon)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false));
    ASSERT_TRUE(llrpHQDualIso(fixture.video()) != 0);
    ASSERT_FALSE(fixture.renderFrame8(0).empty());

    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        std::vector<uint8_t> frame;
        ASSERT_TRUE(reducedPathTaken(fixture, 1, scale, &plan, &frame));
        ASSERT_EQ(scale, plan.scale);
        ASSERT_EQ(fixture.width() / scale, plan.reducedWidth);
        ASSERT_TRUE(plan.reducedHeight > 0);
        ASSERT_TRUE(plan.reducedHeight <= fixture.height() / scale);
        ASSERT_EQ(std::string("none"), std::string(plan.reason));
        ASSERT_EQ(outputBytes(fixture, scale), frame.size());
    }

    // Scale 1 is never reduced.
    mlvDualIsoPreviewScaleRecon_t plan1;
    ASSERT_EQ(0, mlvDualIsoPreviewScaleReconPlan(fixture.video(), 1, &plan1));
    ASSERT_EQ(1, plan1.scale);
}

// (d) Mutation of (b): forcing full resolution in playback (the kill switch that
// production honours) makes exactly the check (b) relies on fail, at both scales.
TEST(CpuDualIsoPreviewScale, ForcingFullResInPlaybackFailsReducedPathCheck)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    ASSERT_TRUE(reducedPathTaken(fixture, 1, 4, nullptr, nullptr));

    CPU_DUALISO_TEST_SETENV(kKillSwitch, "1");
    for (const int scale : { 2, 4 })
    {
        ASSERT_FALSE(reducedPathTaken(fixture, 1, scale, nullptr, nullptr));
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(0, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
        ASSERT_EQ(1, plan.scale);
        ASSERT_TRUE(std::strstr(plan.reason, kKillSwitch) != nullptr);
    }
    // The CUDA backend's GPU recon also forces the full-resolution path.
    CPU_DUALISO_TEST_UNSETENV(kKillSwitch);
    CPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
    ASSERT_FALSE(reducedPathTaken(fixture, 1, 4, nullptr, nullptr));
    CPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
}

// The exposure match is not estimated on reduced data: while histogram matching
// (auto -2) has not settled at full resolution, the plan keeps full resolution.
TEST(CpuDualIsoPreviewScale, UnsettledHistogramMatchKeepsFullResolution)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false));
    fixture.video()->llrawproc->diso_auto_correction = -2;
    fixture.video()->llrawproc->diso_ev_correction = 1;
    fixture.video()->llrawproc->diso_black_delta = -1;
    mlvDualIsoPreviewScaleRecon_t plan;
    ASSERT_EQ(0, mlvDualIsoPreviewScaleReconPlan(fixture.video(), 4, &plan));
    ASSERT_TRUE(std::strstr(plan.reason, "exposure match") != nullptr);
}

// (c) Tolerance against the downscaled full-res recon (the pre-card playback frame
// at the same preview scale), on the large fixture. Metric, on the 8-bit preview
// over the rows both frames reconstruct:
//  - per-channel mean ratio reduced/full within 1 +/- 0.03 (a cast moves R and B
//    against G; the x2/x4 magenta casts that shipped before were > 10 %),
//  - R/G and B/G mean ratios within 0.03 of the full recon's (cast, independent of
//    overall exposure),
//  - PSNR >= 24 dB and mean absolute error <= 6 code values (structure; the reduced
//    recon decimates whole 4-row ISO blocks, so it aliases rather than blurs).
TEST(CpuDualIsoPreviewScale, ReducedPreviewWithinToleranceOfDownscaledFullRecon)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, true));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());

    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        std::vector<uint8_t> reduced;
        std::vector<uint8_t> full;
        ASSERT_TRUE(reducedPathTaken(fixture, 2, scale, &plan, &reduced));
        ASSERT_TRUE(fullReconPreview(fixture, 2, scale, &full));
        ASSERT_EQ(full.size(), reduced.size());

        const size_t rowBytes = static_cast<size_t>(plan.reducedWidth) * 3u;
        const size_t compared = rowBytes * static_cast<size_t>(plan.reducedHeight);
        double sumReduced[3] = { 0.0, 0.0, 0.0 };
        double sumFull[3] = { 0.0, 0.0, 0.0 };
        double sumAbs = 0.0;
        double sumSq = 0.0;
        for (size_t i = 0; i < compared; ++i)
        {
            const double r = reduced[i];
            const double f = full[i];
            sumReduced[i % 3] += r;
            sumFull[i % 3] += f;
            sumAbs += std::fabs(r - f);
            sumSq += (r - f) * (r - f);
        }
        const double mae = sumAbs / static_cast<double>(compared);
        const double mse = sumSq / static_cast<double>(compared);
        const double psnr = mse > 0.0 ? 10.0 * std::log10(255.0 * 255.0 / mse) : 99.0;
        double ratio[3];
        for (int c = 0; c < 3; ++c) ratio[c] = sumFull[c] > 0.0 ? sumReduced[c] / sumFull[c] : 1.0;
        const double rgReduced = sumReduced[0] / sumReduced[1];
        const double rgFull = sumFull[0] / sumFull[1];
        const double bgReduced = sumReduced[2] / sumReduced[1];
        const double bgFull = sumFull[2] / sumFull[1];
        std::printf("[cpu-dualiso-preview-scale] x%d reduced %dx%d: mean ratio R=%.4f G=%.4f B=%.4f "
                    "R/G %.4f vs %.4f B/G %.4f vs %.4f MAE=%.3f PSNR=%.2f dB\n",
                    scale, plan.reducedWidth, plan.reducedHeight, ratio[0], ratio[1], ratio[2],
                    rgReduced, rgFull, bgReduced, bgFull, mae, psnr);
        for (int c = 0; c < 3; ++c) ASSERT_NEAR(1.0, ratio[c], 0.03);
        ASSERT_NEAR(rgFull, rgReduced, 0.03);
        ASSERT_NEAR(bgFull, bgReduced, 0.03);
        ASSERT_TRUE(psnr >= 24.0);
        ASSERT_TRUE(mae <= 6.0);
    }
}

// (a)+(e) A reduced-scale playback session in this process leaves the shared
// llrawproc state exactly where the full-res render left it, and the export path
// (getMlvProcessedFrame8/16, never the preview entries) then produces the same
// bytes as a fresh object that never played.
TEST(CpuDualIsoPreviewScale, ExportAfterReducedPlaybackEqualsFreshProcessExport)
{
    KillSwitchGuard guard;
    MlvPipelineFixture played;
    ASSERT_TRUE(openHqFixture(played, false));
    ASSERT_FALSE(played.renderFrame8(0).empty());
    const SharedDualIsoState settled = sharedState(played);
    for (const uint64_t frame : { 0u, 1u })  // the tiny fixture has two frames
    {
        ASSERT_TRUE(reducedPathTaken(played, frame, 4, nullptr, nullptr));
        ASSERT_TRUE(reducedPathTaken(played, frame, 2, nullptr, nullptr));
    }
    const SharedDualIsoState after = sharedState(played);
    ASSERT_EQ(settled.pattern, after.pattern);
    ASSERT_EQ(settled.autoCorrection, after.autoCorrection);
    ASSERT_EQ(settled.evCorrection, after.evCorrection);
    ASSERT_EQ(settled.blackDelta, after.blackDelta);
    const std::vector<uint8_t> played8 = played.renderFrame8(1);
    const std::vector<uint16_t> played16 = played.renderFrame16(1);

    MlvPipelineFixture fresh;
    ASSERT_TRUE(openHqFixture(fresh, false));
    ASSERT_FALSE(fresh.renderFrame8(0).empty());
    const std::vector<uint8_t> fresh8 = fresh.renderFrame8(1);
    const std::vector<uint16_t> fresh16 = fresh.renderFrame16(1);

    ASSERT_FALSE(fresh8.empty());
    ASSERT_TRUE(played8 == fresh8);
    ASSERT_FALSE(fresh16.empty());
    ASSERT_TRUE(played16 == fresh16);
}

// (e) The scaled llrawproc subset estimates the exposure match from whatever it is
// handed; with NO_PUBLISH that estimate never reaches the shared object. Mutation:
// the same call without the flag publishes (this is why the plan also refuses an
// unsettled match).
TEST(CpuDualIsoPreviewScale, ScaledSubsetNoPublishKeepsSharedExposureMatch)
{
    KillSwitchGuard guard;
    for (const bool publish : { false, true })
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openHqFixture(fixture, false));
        const int fullW = fixture.width();
        const int sourceH = (fixture.height() / 16) * 16;
        std::vector<uint16_t> raw = decodeRaw(fixture, 0);
        ASSERT_FALSE(raw.empty());
        llrpEnsureDualIsoPatternSeeded(fixture.video(), raw.data(), fullW, fixture.height());
        fixture.video()->llrawproc->diso_auto_correction = -2;
        fixture.video()->llrawproc->diso_ev_correction = 1;
        fixture.video()->llrawproc->diso_black_delta = -1;
        std::vector<uint16_t> reduced(static_cast<size_t>(fullW / 4) * static_cast<size_t>(sourceH / 4));
        int w = 0, h = 0;
        ASSERT_EQ(0, pl_downsample_bayer_to_bayer_4x(raw.data(), fullW, sourceH, reduced.data(), &w, &h, 1));
        WorkerState worker;
        const int flags = LLRP_WITH_DIMS_FULLRES_FIXES_APPLIED | (publish ? 0 : LLRP_WITH_DIMS_NO_PUBLISH);
        ASSERT_EQ(1, applyLLRawProcObjectWorker_with_dims(fixture.video(), reduced.data(),
                                                          reduced.size() * sizeof(uint16_t), w, h,
                                                          &worker.state, flags));
        const SharedDualIsoState s = sharedState(fixture);
        if (publish)
        {
            ASSERT_TRUE(s.evCorrection != 1.0 || s.blackDelta != -1);
        }
        else
        {
            ASSERT_EQ(1.0, s.evCorrection);
            ASSERT_EQ(-1, s.blackDelta);
            ASSERT_EQ(-2, s.autoCorrection);
        }
    }
}

// (f) The reduced frame is published under a signature that no full-recon lookup
// can match, so the current-frame signature (which the GUI display path keys on)
// differs between the reduced and the full recon of the same frame, and a later
// full-path render of that frame at the same scale (pause / scrub) is not served
// the reduced pixels: it equals the same render on a fresh object.
TEST(CpuDualIsoPreviewScale, ReducedReconUsesDistinctProcessedFrameSignature)
{
    KillSwitchGuard guard;
    for (const int scale : { 2, 4 })
    {
        const uint64_t base = 0x0123456789abcdefull;
        ASSERT_NE(base, mlvReducedReconProcessedFrameSignature(base, scale));
        ASSERT_NE(mlvReducedReconProcessedFrameSignature(base, 2), mlvReducedReconProcessedFrameSignature(base, 4));
    }

    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    ASSERT_TRUE(reducedPathTaken(fixture, 1, 4, nullptr, nullptr));
    const uint64_t fullSignature = mlvProcessedFrameSignatureWithScale(fixture.video(), 1, 4);
    ASSERT_EQ(1, fixture.video()->current_processed_frame_8bit_active);
    ASSERT_EQ(mlvReducedReconProcessedFrameSignature(fullSignature, 4),
              fixture.video()->current_processed_frame_8bit_signature);
    ASSERT_NE(fullSignature, fixture.video()->current_processed_frame_8bit_signature);

    std::vector<uint8_t> afterReduced(outputBytes(fixture, 4));
    getMlvProcessedFrame8Scaled(fixture.video(), 1, afterReduced.data(), 1, 4);

    MlvPipelineFixture fresh;
    ASSERT_TRUE(openHqFixture(fresh, false));
    ASSERT_FALSE(fresh.renderFrame8(0).empty());
    std::vector<uint8_t> freshFrame(outputBytes(fresh, 4));
    getMlvProcessedFrame8Scaled(fresh.video(), 1, freshFrame.data(), 1, 4);
    ASSERT_TRUE(afterReduced == freshFrame);
}
