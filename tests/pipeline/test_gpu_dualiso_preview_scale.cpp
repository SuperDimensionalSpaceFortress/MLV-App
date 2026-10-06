// PLAYBACK-CUDA-HONOUR-SCALE-1: the CUDA texture route honours preview scale 2/4 by
// running the existing GPU prepare + preupload + retained-device recon on the
// Phase-4B-shrunk Bayer (mlvDualIsoGpuPreviewScaleReconPlan / ...Run, the GPU sibling
// of CPU-DUALISO-AT-PREVIEW-SCALE-1's plan). Hosted CI has no GPU, so these tests use
// the in-process fake backend behind the igpu_recon_backend C seam (output = input+1)
// and pin:
//  (b) the GPU plan admits x2/x4 only for GPU playback recon, keeps x8, scale 1 and
//      every CPU-plan refusal on full resolution, and the CPU plan's own
//      "GPU playback recon requested" refusal is unchanged;
//  (g) a recon at new dimensions while a retained device output is still in use is
//      refused by the C seam, so set_clip (which frees EVERY retained output) never
//      frees one a presenter may be reading; once released, the new size proceeds;
//  the route itself: the retained output is the backend's recon of exactly the
//      reduced prepared Bayer, at the reduced dims, and nothing is published;
//  (f) an export in the same process after a reduced CUDA playback equals a fresh
//      object's export (the GPU prepare writes auto/ev/black through pointers).
// The pixel parity of the real CUDA kernels at reduced dims ((c1), (c2)) is a venue
// proof; see the PR.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include "../../src/processing/raw_processing.h"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#define GPU_DUALISO_TEST_SETENV(name, value) _putenv_s((name), (value))
#define GPU_DUALISO_TEST_UNSETENV(name) _putenv_s((name), "")
#else
#define GPU_DUALISO_TEST_SETENV(name, value) setenv((name), (value), 1)
#define GPU_DUALISO_TEST_UNSETENV(name) unsetenv((name))
#endif

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" int llrpFakeGpuBackendFreedInUseRetainedForTesting(void);

namespace {

const char * const kGpuKillSwitch = "MLVAPP_DISABLE_GPU_DUALISO_PREVIEW_SCALE_RECON";

struct GpuReconEnv
{
    explicit GpuReconEnv(bool gpu)
    {
        GPU_DUALISO_TEST_UNSETENV(kGpuKillSwitch);
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON");
        GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_EXPORT", "0");
        GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", "0");
        GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT", "0");
        GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT", "1");
        if (gpu) GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
        else GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
    }
    ~GpuReconEnv()
    {
        GPU_DUALISO_TEST_UNSETENV(kGpuKillSwitch);
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_EXPORT");
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT");
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT");
    }
};

struct FakeGpuBackendScope
{
    FakeGpuBackendScope() { llrpInstallFakeGpuPlaybackReconBackendForTesting(1); }
    ~FakeGpuBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

// The recon worker's thread opt-ins for a texture-route frame
// (GpuPlaybackReconScope, ...TexturePresentScope, ...PrepareOnlyScope).
struct TextureRouteThreadOptIn
{
    TextureRouteThreadOptIn()
    {
        llrpSetGpuPlaybackReconAllowedForCurrentThread(1);
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(1);
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(1);
    }
    ~TextureRouteThreadOptIn()
    {
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(0);
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(0);
        llrpSetGpuPlaybackReconAllowedForCurrentThread(0);
    }
};

struct WorkerState
{
    WorkerState() { llrpInitWorkerState(&state); }
    ~WorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

bool openGpuEligibleFixture(MlvPipelineFixture & fixture)
{
    QString error;
    if (!fixture.openTinyDualIso(&error)) return false;
    if (!fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error)) return false;
    if (!fixture.applyReceipt(&error)) return false;
    // The validated no-readback recon class (test_async_preupload_pipeline.cpp).
    mlvObject_t * video = fixture.video();
    llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
    llrpSetDualIsoAliasMapMode(video, FR_ON);
    llrpSetDualIsoFullResBlendingMode(video, FR_ON);
    llrpSetChromaSmoothMode(video, CS_OFF);
    video->llrawproc->focus_pixels = 0;
    video->llrawproc->bad_pixels = 0;
    video->llrawproc->vertical_stripes = 0;
    return true;
}

std::vector<uint16_t> decodeRaw(MlvPipelineFixture & fixture, uint64_t frame)
{
    std::vector<uint16_t> raw(static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height()));
    if (getMlvRawFrameUint16(fixture.video(), frame, raw.data()) != 0) raw.clear();
    return raw;
}

llrpGpuPlaybackReconState_t validatedState(uint64_t frameId, int width, int height)
{
    static int dummy_int_lut[1] = { 0 };
    static double dummy_double_lut[1] = { 0.0 };
    llrpGpuPlaybackReconState_t state = {};
    state.valid = 1;
    state.width = width;
    state.height = height;
    state.white_level = 65535;
    state.white_darkened = 65535;
    state.interp_method = 1;
    state.use_alias_map = 1;
    state.use_fullres = 1;
    state.raw2ev = dummy_int_lut;
    state.ev2raw = dummy_int_lut;
    state.mix_curve = dummy_double_lut;
    state.fullres_curve = dummy_double_lut;
    state.frame_id = frameId;
    return state;
}

struct GpuReducedFrame
{
    mlvDualIsoPreviewScaleRecon_t plan;
    std::vector<uint16_t> preparedInput;
    std::vector<uint16_t> retained;
    llrpGpuPlaybackRetainedDeviceBayer16_t device;
};

// What the recon worker does for one honoured-session frame: plan (latched),
// shrink + GPU texture route, then read back the retained output (host memory
// on the fake backend). The token is released before returning.
bool gpuReducedFrame(MlvPipelineFixture & fixture, uint64_t frame, int scale, GpuReducedFrame * out)
{
    GpuReducedFrame result;
    if (!mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), scale, 1, &result.plan)
        || result.plan.scale != scale)
    {
        std::printf("[gpu-dualiso-preview-scale] x%d full res: %s\n", scale, result.plan.reason);
        return false;
    }
    std::vector<uint16_t> raw = decodeRaw(fixture, frame);
    if (raw.empty()) return false;
    result.preparedInput.assign(static_cast<size_t>(result.plan.reducedWidth)
                                * static_cast<size_t>(result.plan.reducedHeight), 0u);
    WorkerState worker;
    const TextureRouteThreadOptIn optIn;
    mlv_pipeline_capture_set_current_frame(frame);
    const int rc = mlvDualIsoGpuPreviewScaleReconRun(fixture.video(), &result.plan, raw.data(),
                                                     result.preparedInput.data(), &worker.state,
                                                     1, nullptr, nullptr);
    if (rc != 1)
    {
        std::printf("[gpu-dualiso-preview-scale] x%d run rc=%d backend rc=%d\n",
                    scale, rc, llrpGpuPlaybackReconLastRunRcForTesting());
        return false;
    }
    std::memset(&result.device, 0, sizeof(result.device));
    if (!llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(&result.device)
        || !result.device.valid || !result.device.device_bayer16)
    {
        return false;
    }
    result.retained.assign(result.device.device_bayer16,
                           result.device.device_bayer16 + result.preparedInput.size());
    llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(result.device.token);
    if (out) *out = result;
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

// (b) The GPU plan is the CUDA texture route's sibling of the CPU plan.
TEST(GpuDualIsoPreviewScale, PlanAdmitsX2AndX4OnlyForGpuPlaybackRecon)
{
    {
        GpuReconEnv env(false);
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openGpuEligibleFixture(fixture));
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 4, 0, &plan));
        ASSERT_EQ(std::string("GPU playback recon not requested"), std::string(plan.reason));
    }

    GpuReconEnv env(true);
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openGpuEligibleFixture(fixture));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(1, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), scale, 0, &plan));
        ASSERT_EQ(scale, plan.scale);
        ASSERT_EQ(fixture.width() / scale, plan.reducedWidth);
        ASSERT_EQ((fixture.height() / (4 * scale)) * 4, plan.reducedHeight);
        ASSERT_EQ(std::string("none"), std::string(plan.reason));
    }
    mlvDualIsoPreviewScaleRecon_t plan;
    ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 8, 0, &plan));
    ASSERT_EQ(1, plan.scale);
    ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 1, 0, &plan));
    ASSERT_EQ(1, plan.scale);

    // The CPU plan's refusal under GPU playback recon is unchanged (B1).
    ASSERT_EQ(0, mlvDualIsoPreviewScaleReconPlan(fixture.video(), 4, &plan));
    ASSERT_EQ(std::string("GPU playback recon requested"), std::string(plan.reason));

    // Kill switch.
    GPU_DUALISO_TEST_SETENV(kGpuKillSwitch, "1");
    ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 4, 0, &plan));
    ASSERT_TRUE(std::strstr(plan.reason, kGpuKillSwitch) != nullptr);
    GPU_DUALISO_TEST_UNSETENV(kGpuKillSwitch);

    // An unsettled exposure match refuses the session decision, but a session
    // already latched keeps its dims (the frame size never changes mid-session).
    fixture.video()->llrawproc->diso_auto_correction = -2;
    fixture.video()->llrawproc->diso_ev_correction = 1;
    fixture.video()->llrawproc->diso_black_delta = -1;
    ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 4, 0, &plan));
    ASSERT_TRUE(std::strstr(plan.reason, "exposure match") != nullptr);
    ASSERT_EQ(1, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 4, 1, &plan));
    ASSERT_EQ(4, plan.scale);
}

// (g) igpu_recon_set_clip frees every retained output, in use or not. The C seam
// refuses a run that needs new clip dimensions while any is outstanding.
// Mutation: drop the guard in llrawproc_gpu_recon_run_backend and the fake's
// freed-in-use counter becomes 1 here.
TEST(GpuDualIsoPreviewScale, DimsChangeNeverFreesAnInUseRetainedToken)
{
    GpuReconEnv env(true);
    const FakeGpuBackendScope fake;
    const int fullW = 16, fullH = 16, reducedW = 8, reducedH = 8;
    std::vector<uint16_t> full(static_cast<size_t>(fullW) * fullH, 100u);
    std::vector<uint16_t> reduced(static_cast<size_t>(reducedW) * reducedH, 200u);
    llrpGpuPlaybackReconState_t fullState = validatedState(1, fullW, fullH);
    llrpGpuPlaybackReconState_t reducedState = validatedState(2, reducedW, reducedH);
    llrpGpuPlaybackReconTiming_t timing = {};
    int rc = -1;
    const uint64_t refusalsBefore = llrpGpuPlaybackReconClipDimsChangeRefusals();

    llrpGpuPlaybackRetainedDeviceBayer16_t inFlight = {};
    ASSERT_NE(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&fullState, full.data(),
        full.size() * sizeof(uint16_t), &inFlight, &rc, &timing));
    ASSERT_NE(0, inFlight.valid);
    ASSERT_EQ(1, llrpGpuPlaybackReconRetainedOutstandingCount());

    // A presenter still holds `inFlight`: the reduced size is refused, nothing freed.
    llrpGpuPlaybackRetainedDeviceBayer16_t refused = {};
    ASSERT_EQ(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&reducedState, reduced.data(),
        reduced.size() * sizeof(uint16_t), &refused, &rc, &timing));
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CLIP_DIMS_BUSY, rc);
    ASSERT_EQ(0, refused.valid);
    ASSERT_EQ(0, llrpFakeGpuBackendFreedInUseRetainedForTesting());
    ASSERT_EQ(refusalsBefore + 1, llrpGpuPlaybackReconClipDimsChangeRefusals());

    // The same size still runs while the token is out (no set_clip needed).
    llrpGpuPlaybackRetainedDeviceBayer16_t sameSize = {};
    ASSERT_NE(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&fullState, full.data(),
        full.size() * sizeof(uint16_t), &sameSize, &rc, &timing));
    ASSERT_NE(inFlight.token, sameSize.token);
    ASSERT_EQ(2, llrpGpuPlaybackReconRetainedOutstandingCount());

    // Released: the new size proceeds, and still nothing in use was freed.
    ASSERT_NE(0, llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(inFlight.token));
    ASSERT_NE(0, llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(sameSize.token));
    ASSERT_EQ(0, llrpGpuPlaybackReconRetainedOutstandingCount());
    llrpGpuPlaybackRetainedDeviceBayer16_t resized = {};
    ASSERT_NE(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&reducedState, reduced.data(),
        reduced.size() * sizeof(uint16_t), &resized, &rc, &timing));
    ASSERT_NE(0, resized.valid);
    ASSERT_EQ(reducedW, resized.width);
    ASSERT_EQ(reducedH, resized.height);
    ASSERT_EQ(0, llrpFakeGpuBackendFreedInUseRetainedForTesting());
    ASSERT_NE(0, llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(resized.token));
}

// The route: x2 and x4 reconstruct exactly the reduced prepared Bayer on the
// backend, at the reduced dims, publishing nothing to the shared state.
TEST(GpuDualIsoPreviewScale, ReducedTextureRouteReconstructsTheReducedBayer)
{
    GpuReconEnv env(true);
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openGpuEligibleFixture(fixture));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    const SharedDualIsoState settled = sharedState(fixture);
    const FakeGpuBackendScope fake;
    for (const int scale : { 4, 2 })
    {
        GpuReducedFrame frame;
        ASSERT_TRUE(gpuReducedFrame(fixture, 1, scale, &frame));
        ASSERT_EQ(frame.plan.reducedWidth, frame.device.width);
        ASSERT_EQ(frame.plan.reducedHeight, frame.device.height);
        llrpGpuPlaybackReconState_t prepared = {};
        ASSERT_NE(0, llrpGpuPlaybackReconGetLastPreparedState(&prepared));
        ASSERT_EQ(frame.plan.reducedWidth, prepared.width);
        ASSERT_EQ(frame.plan.reducedHeight, prepared.height);
        ASSERT_EQ(frame.preparedInput.size(), frame.retained.size());
        for (size_t i = 0; i < frame.retained.size(); ++i)
        {
            ASSERT_EQ(static_cast<uint16_t>(frame.preparedInput[i] + 1u), frame.retained[i]);
        }
    }
    ASSERT_EQ(0, llrpGpuPlaybackReconRetainedOutstandingCount());
    ASSERT_EQ(0, llrpFakeGpuBackendFreedInUseRetainedForTesting());

    // Inside the playback preview envelope the reduced recon is prepared with the
    // x1 playback hint (it is the x1 recon of a smaller image; the fullres mesh
    // guard keys on hint 1), never with the preview scale: the first Bachelor
    // leg showed the 4-row ISO mesh with the scale passed through.
    {
        const int previousMode = processingPlaybackPreviewModeEnabled();
        const int previousScale = processingPlaybackPreviewScaleFactor();
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackPreviewScaleFactor(4);
        fixture.video()->playback_scale_factor_active = 4;
        GpuReducedFrame frame;
        const bool ran = gpuReducedFrame(fixture, 1, 4, &frame);
        llrpGpuPlaybackReconState_t prepared = {};
        const int havePrepared = llrpGpuPlaybackReconGetLastPreparedState(&prepared);
        processingSetPlaybackPreviewScaleFactor(previousScale);
        processingSetPlaybackPreviewMode(previousMode);
        ASSERT_TRUE(ran);
        ASSERT_NE(0, havePrepared);
        ASSERT_EQ(1, prepared.playback_preview_scale_factor);
    }
    const SharedDualIsoState after = sharedState(fixture);
    ASSERT_EQ(settled.pattern, after.pattern);
    ASSERT_EQ(settled.autoCorrection, after.autoCorrection);
    ASSERT_EQ(settled.evCorrection, after.evCorrection);
    ASSERT_EQ(settled.blackDelta, after.blackDelta);
}

// The 4-row ISO residual. Dual-ISO reconstruction leaves a small level residual
// with the ISO row period (4 rows) at ANY size; the first x4 venue legs showed it
// magnified (4 texture rows of a 452x564 frame are ~11 display pixels, against
// ~3 for the full-resolution frame, whose own residual measures the same). This
// pins that the reduced recon adds none: its period-4 row energy is within 2x of
// the full recon's at the full recon's own period. The reduced recon here is the
// CPU one (the shrink is the GPU route's; CUDA matches the CPU recon bit for bit).
namespace {
double rowPeriod4Energy(const std::vector<uint8_t> & rgb, int w, int h)
{
    std::vector<double> rows(static_cast<size_t>(h), 0.0);
    for (int y = 0; y < h; ++y)
    {
        double s = 0.0;
        for (int x = 0; x < w * 3; ++x) s += rgb[static_cast<size_t>(y) * w * 3 + x];
        rows[static_cast<size_t>(y)] = s / (w * 3.0);
    }
    // Remove the local mean (5-row box), then correlate with the period-4 basis.
    double re = 0.0, im = 0.0;
    int n = 0;
    for (int y = 2; y < h - 2; ++y)
    {
        const double local = (rows[y - 2] + rows[y - 1] + rows[y] + rows[y + 1] + rows[y + 2]) / 5.0;
        const double v = rows[static_cast<size_t>(y)] - local;
        re += v * std::cos(2.0 * 3.14159265358979 * y / 4.0);
        im += v * std::sin(2.0 * 3.14159265358979 * y / 4.0);
        ++n;
    }
    return n > 0 ? std::sqrt(re * re + im * im) / n : 0.0;
}
} // namespace

TEST(GpuDualIsoPreviewScale, ReducedReconAddsNoIsoPeriodResidual)
{
    GpuReconEnv env(false);
    MlvPipelineFixture fixture;
    QString error;
    ASSERT_TRUE(fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"), &error));
    ASSERT_TRUE(fixture.applyReceipt(&error));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    const std::vector<uint8_t> fullFrame = fixture.renderFrame8(2);
    ASSERT_FALSE(fullFrame.empty());
    const double meshFull = rowPeriod4Energy(fullFrame, fixture.width(), fixture.height());
    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
        int w = 0, h = 0;
        mlvFrameOutputDimensions(fixture.video(), scale, &w, &h);
        std::vector<uint8_t> reduced(static_cast<size_t>(w) * h * 3);
        std::vector<uint16_t> raw = decodeRaw(fixture, 2);
        ASSERT_FALSE(raw.empty());
        std::vector<uint16_t> bayer(static_cast<size_t>(plan.reducedWidth) * plan.reducedHeight);
        WorkerState worker;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(fixture.video(), &plan, raw.data(), bayer.data(),
                                                    &worker.state, 1, nullptr, nullptr));
        const int previousMode = processingPlaybackPreviewModeEnabled();
        const int previousScale = processingPlaybackPreviewScaleFactor();
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackPreviewScaleFactor(scale);
        const int ok = getMlvProcessedFrame8ScaledFromReducedReconnedRaw16(
            fixture.video(), 2, bayer.data(), plan.reducedWidth, plan.reducedHeight, plan.scale,
            reduced.data(), 1, scale);
        processingSetPlaybackPreviewScaleFactor(previousScale);
        processingSetPlaybackPreviewMode(previousMode);
        ASSERT_EQ(1, ok);
        const double meshReduced = rowPeriod4Energy(reduced, w, plan.reducedHeight);
        std::printf("[gpu-dualiso-preview-scale] x%d period-4 row energy reduced=%.4f full-res=%.4f\n",
                    scale, meshReduced, meshFull);
        ASSERT_TRUE(meshReduced <= meshFull * 2.0 + 0.05);
    }
}
// (f) Export after a reduced CUDA playback session in this process equals a fresh
// object's export, 8- and 16-bit.
TEST(GpuDualIsoPreviewScale, ExportAfterReducedGpuPlaybackEqualsFreshExport)
{
    std::vector<uint8_t> played8;
    std::vector<uint16_t> played16;
    {
        GpuReconEnv env(true);
        MlvPipelineFixture played;
        ASSERT_TRUE(openGpuEligibleFixture(played));
        ASSERT_FALSE(played.renderFrame8(0).empty());
        {
            const FakeGpuBackendScope fake;
            for (const uint64_t frame : { 0u, 1u })
            {
                ASSERT_TRUE(gpuReducedFrame(played, frame, 4, nullptr));
                ASSERT_TRUE(gpuReducedFrame(played, frame, 2, nullptr));
            }
        }
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
        played8 = played.renderFrame8(1);
        played16 = played.renderFrame16(1);
    }

    GpuReconEnv env(false);
    MlvPipelineFixture fresh;
    ASSERT_TRUE(openGpuEligibleFixture(fresh));
    ASSERT_FALSE(fresh.renderFrame8(0).empty());
    const std::vector<uint8_t> fresh8 = fresh.renderFrame8(1);
    const std::vector<uint16_t> fresh16 = fresh.renderFrame16(1);
    ASSERT_FALSE(fresh8.empty());
    ASSERT_TRUE(played8 == fresh8);
    ASSERT_FALSE(fresh16.empty());
    ASSERT_TRUE(played16 == fresh16);
}
