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
// r2: the reduced run ends with the same-colour ISO-period notch
// (dualiso_reduced_iso_period_notch16, ported line for line to the CUDA backend's
// last kernel), switched by the optional igpu_recon_set_reduced_iso_notch symbol.
// r3: the reduced texture's AMaZE RGB16 then takes a horizontal [1,2,1]/4 Nyquist zero
// (debayer_reduced_hnyquist121_rgb16, ported line for line to the CUDA AMaZE backend,
// switched by the optional igpu_amaze_debayer_set_reduced_hnyquist symbol); T3 emulates
// it on the reduced presentation's RGB16 and adds L8 (column Nyquist) and L9.
// The pixel parity of the real CUDA kernels at reduced dims ((c1), (c2)) is a venue
// proof; see the PR.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/llrawproc/dualiso.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/debayer/debayer.h"

#include <QDir>

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
extern "C" int llrpSetFakeGpuBackendReducedIsoNotchSymbolForTesting(int present);
extern "C" void llrpFakeGpuBackendReducedIsoNotchStateForTesting(int * last_run_flag,
                                                                 uint64_t * runs_with_notch,
                                                                 uint64_t * runs_without_notch,
                                                                 uint64_t * set_calls);

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
        // r2: the fake's recon (input + 1) followed by the reduced ISO notch.
        std::vector<uint16_t> fakeRecon(frame.preparedInput.size());
        for (size_t i = 0; i < fakeRecon.size(); ++i)
        {
            fakeRecon[i] = static_cast<uint16_t>(frame.preparedInput[i] + 1u);
        }
        std::vector<uint16_t> expected(fakeRecon.size());
        dualiso_reduced_iso_period_notch16(expected.data(), fakeRecon.data(),
                                           frame.plan.reducedWidth, frame.plan.reducedHeight);
        ASSERT_TRUE(expected == frame.retained);
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

// The 4-row ISO residual. Dual-ISO reconstruction leaves a level residual with the
// ISO row period (4 rows) at ANY size. The full-resolution route box-downsamples its
// recon by the preview scale, which averages that residual away (0.0003 on this
// fixture); a reduced recon presented as-is shows it magnified (0.19 at x2, 0.17 at
// x4), the visible ISO-period mesh of the r1b x2/x4 CUDA venue contact frames. r2
// removes it with the same-colour [1,2,1]/4 notch on the reduced Bayer16, before
// debayer. The reduced recon here is the CPU one plus the C reference notch; the
// CUDA reduced route runs the same recon and the notch as its last kernel, and
// their byte parity is the venue proof (c1, tools/gpu/cuda_recon_parity.cu).
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

// L2: the same estimator per column on the per-pixel channel mean, averaged
// across columns (catches the 2-D dots a row average would hide).
double columnPeriod4Energy(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    std::vector<double> col(static_cast<size_t>(h), 0.0);
    for (int x = 0; x < w; ++x)
    {
        for (int y = 0; y < h; ++y)
        {
            const size_t i = (static_cast<size_t>(y) * w + x) * 3;
            col[static_cast<size_t>(y)] = (rgb[i] + rgb[i + 1] + rgb[i + 2]) / 3.0;
        }
        double re = 0.0, im = 0.0;
        int n = 0;
        for (int y = 2; y < h - 2; ++y)
        {
            const double local = (col[y - 2] + col[y - 1] + col[y] + col[y + 1] + col[y + 2]) / 5.0;
            const double v = col[static_cast<size_t>(y)] - local;
            re += v * std::cos(2.0 * 3.14159265358979 * y / 4.0);
            im += v * std::sin(2.0 * 3.14159265358979 * y / 4.0);
            ++n;
        }
        sum += n > 0 ? std::sqrt(re * re + im * im) / n : 0.0;
    }
    return w > 0 ? sum / w : 0.0;
}

// L3: lag-4 vertical luma detail, mean |L(x,y+4) - L(x,y)| (blind to period 4).
double lag4VerticalLumaDetail(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    size_t n = 0;
    auto luma = [&](int x, int y) {
        const size_t i = (static_cast<size_t>(y) * w + x) * 3;
        return 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2];
    };
    for (int y = 0; y + 4 < h; ++y)
    {
        for (int x = 0; x < w; ++x)
        {
            sum += std::fabs(luma(x, y + 4) - luma(x, y));
            ++n;
        }
    }
    return n ? sum / static_cast<double>(n) : 0.0;
}

double channelMean(const std::vector<uint8_t> & rgb, int w, int h, int c)
{
    double sum = 0.0;
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) sum += rgb[(static_cast<size_t>(y) * w + x) * 3 + c];
    return sum / (static_cast<double>(w) * h);
}

// The full-res route's preview: its 8-bit frame box-downsampled by `scale`.
std::vector<uint8_t> boxDownsample(const std::vector<uint8_t> & full, int fullW, int scale,
                                   int w, int h)
{
    std::vector<uint8_t> out(static_cast<size_t>(w) * h * 3);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
            for (int c = 0; c < 3; ++c)
            {
                int s = 0;
                for (int dy = 0; dy < scale; ++dy)
                    for (int dx = 0; dx < scale; ++dx)
                        s += full[((static_cast<size_t>(y) * scale + dy) * static_cast<size_t>(fullW)
                                   + static_cast<size_t>(x) * scale + dx) * 3 + c];
                out[(static_cast<size_t>(y) * w + x) * 3 + c] =
                    static_cast<uint8_t>((s + scale * scale / 2) / (scale * scale));
            }
    return out;
}

// L8 (r3): colNyqEnergy, rowPeriod4Energy's estimator along x at period 2. Per row,
// on the per-pixel channel mean: remove the local mean (5-sample box), correlate with
// the period-2 basis cos(pi x), take the amplitude A, normalise as (A / row mean)^2,
// and average over rows.
double colNyqEnergy(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    int rowsUsed = 0;
    std::vector<double> v(static_cast<size_t>(w), 0.0);
    for (int y = 0; y < h; ++y)
    {
        double mean = 0.0;
        for (int x = 0; x < w; ++x)
        {
            const size_t i = (static_cast<size_t>(y) * w + x) * 3;
            v[static_cast<size_t>(x)] = (rgb[i] + rgb[i + 1] + rgb[i + 2]) / 3.0;
            mean += v[static_cast<size_t>(x)];
        }
        mean /= w;
        if (mean <= 0.0 || w < 5) continue;
        double re = 0.0;
        int n = 0;
        for (int x = 2; x < w - 2; ++x)
        {
            const double local = (v[x - 2] + v[x - 1] + v[x] + v[x + 1] + v[x + 2]) / 5.0;
            re += (v[static_cast<size_t>(x)] - local) * ((x % 2 == 0) ? 1.0 : -1.0);
            ++n;
        }
        const double a = n > 0 ? std::fabs(re) / n : 0.0;
        sum += (a / mean) * (a / mean);
        ++rowsUsed;
    }
    return rowsUsed > 0 ? sum / rowsUsed : 0.0;
}

// L9 (r3): lag-2 horizontal luma detail, mean |L(x+2,y) - L(x,y)| (blind to period 2).
double lag2HorizontalLumaDetail(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    size_t n = 0;
    auto luma = [&](int x, int y) {
        const size_t i = (static_cast<size_t>(y) * w + x) * 3;
        return 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2];
    };
    for (int y = 0; y < h; ++y)
        for (int x = 0; x + 2 < w; ++x)
        {
            sum += std::fabs(luma(x + 2, y) - luma(x, y));
            ++n;
        }
    return n ? sum / static_cast<double>(n) : 0.0;
}

// The GPU route's reduced presentation, emulated: getMlvProcessedFrame8ScaledFromReduced-
// ReconnedRaw16's steps (debayerBasicU16, last-row padding, applyProcessingObject8) with
// the reduced H-Nyquist filter on the RGB16 between debayer and processing, as the CUDA
// AMaZE backend applies it before its WB-undo pack. Call after that function has run for
// this frame (it syncs the processing levels); with hnyquist=false the output equals it.
std::vector<uint8_t> emulateReducedPresent(MlvPipelineFixture & fixture,
                                           uint64_t frame,
                                           const std::vector<uint16_t> & bayer,
                                           int reducedWidth,
                                           int reducedHeight,
                                           int outHeight,
                                           bool hnyquist)
{
    const size_t rowWords = static_cast<size_t>(reducedWidth) * 3u;
    std::vector<uint16_t> work(bayer);
    std::vector<uint16_t> rgb(rowWords * static_cast<size_t>(outHeight), 0u);
    debayerBasicU16(rgb.data(), work.data(), reducedWidth, reducedHeight, 1, 0);
    for (int y = reducedHeight; y < outHeight; ++y)
        std::memcpy(rgb.data() + static_cast<size_t>(y) * rowWords,
                    rgb.data() + static_cast<size_t>(reducedHeight - 1) * rowWords,
                    rowWords * sizeof(uint16_t));
    if (hnyquist)
    {
        std::vector<uint16_t> filtered(rgb.size(), 0u);
        debayer_reduced_hnyquist121_rgb16(filtered.data(), rgb.data(), reducedWidth, outHeight);
        rgb.swap(filtered);
    }
    std::vector<uint8_t> out(rgb.size(), 0u);
    applyProcessingObject8(fixture.processing(), reducedWidth, outHeight, rgb.data(), out.data(),
                           1, 1, frame);
    return out;
}

// Interleaved RGB16 field, per-channel bases.
std::vector<uint16_t> rgb16Field(int w, int h, int r, int g, int b)
{
    std::vector<uint16_t> f(static_cast<size_t>(w) * h * 3);
    for (size_t i = 0; i < f.size(); i += 3)
    {
        f[i] = static_cast<uint16_t>(r);
        f[i + 1] = static_cast<uint16_t>(g);
        f[i + 2] = static_cast<uint16_t>(b);
    }
    return f;
}

std::vector<uint8_t> rgb16To8(const std::vector<uint16_t> & rgb)
{
    std::vector<uint8_t> out(rgb.size());
    for (size_t i = 0; i < rgb.size(); ++i) out[i] = static_cast<uint8_t>(rgb[i] >> 8);
    return out;
}

// Synthetic RGGB Bayer16: per-colour base plus `amp` alternating in sign every
// same-colour row (a 4-row period), i.e. the dual-ISO residual shape.
std::vector<uint16_t> alternatingBayer(int w, int h, int amp)
{
    const int base[2][2] = { { 1000, 2000 }, { 2100, 3000 } };
    std::vector<uint16_t> bayer(static_cast<size_t>(w) * h);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const int sign = ((y / 2) % 2 == 0) ? 1 : -1;
            bayer[static_cast<size_t>(y) * w + x] =
                static_cast<uint16_t>(base[y % 2][x % 2] + sign * amp);
        }
    return bayer;
}
} // namespace

// T1: the notch's response at the 4-row period is exactly zero, edges included
// (mirrored taps), and it keeps every colour plane's level.
// Mutations: other weights, stride 1, clamped (not mirrored) edge rows.
TEST(GpuDualIsoPreviewScale, NotchNullsSameColourAlternation)
{
    for (const int h : { 16, 18, 14 })
    {
        const int w = 8;
        const std::vector<uint16_t> src = alternatingBayer(w, h, 100);
        std::vector<uint16_t> out(src.size(), 0u);
        dualiso_reduced_iso_period_notch16(out.data(), src.data(), w, h);
        const std::vector<uint16_t> flat = alternatingBayer(w, h, 0);
        for (size_t i = 0; i < out.size(); ++i)
        {
            if (out[i] != flat[i])
                std::printf("[notch] h=%d i=%zu out=%u want=%u\n", h, i, out[i], flat[i]);
            ASSERT_EQ(flat[i], out[i]);
        }
    }
}

// T2: every tap is the pixel's own Bayer colour (2-row stride, same column), the
// weights are [1,-6,15,44,15,-6,1]/64 applied once, and the clamp to the +-2
// neighbours' range stops the negative taps ringing.
// Mutations: stride 1 (G/B pick up the R signal), applied twice, no clamp.
TEST(GpuDualIsoPreviewScale, NotchKeepsColourPlanesSeparate)
{
    const int w = 8, h = 16;
    std::vector<uint16_t> src = alternatingBayer(w, h, 0);
    src[static_cast<size_t>(8) * w + 2] = 1400; // one R pixel (even row, even column)
    std::vector<uint16_t> out(src.size(), 0u);
    dualiso_reduced_iso_period_notch16(out.data(), src.data(), w, h);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const size_t i = static_cast<size_t>(y) * w + x;
            uint16_t want = src[i];
            if (x == 2 && y == 8) want = 1275;                  // (64000 + 44 * 400 + 32) >> 6
            else if (x == 2 && (y == 6 || y == 10)) want = 1094; // (64000 + 15 * 400 + 32) >> 6
            else if (y % 2 == 0 && x % 2 == 0) want = 1000;      // 963 / 1006 clamped to 1000
            ASSERT_EQ(want, out[i]);
        }

    // A B-plane step 3000 -> 9000 at row 9: only the two rows beside the edge move
    // (3938, 8063); rows 3, 5, 11 and 13 would ring (3188, 2625, 9375, 8813)
    // without the clamp.
    std::vector<uint16_t> step = alternatingBayer(w, h, 0);
    for (int y = 9; y < h; y += 2)
        for (int x = 1; x < w; x += 2) step[static_cast<size_t>(y) * w + x] = 9000;
    dualiso_reduced_iso_period_notch16(out.data(), step.data(), w, h);
    std::vector<uint16_t> stepWant = step;
    for (int x = 1; x < w; x += 2)
    {
        stepWant[static_cast<size_t>(7) * w + x] = 3938;
        stepWant[static_cast<size_t>(9) * w + x] = 8063;
    }
    ASSERT_TRUE(out == stepWant);
}

// T3 (L1-L4, L8, L9): the CPU reduced recon (CUDA matches it, c1) plus the C reference
// notch, presented through the reduced consumer with the C reference H-Nyquist filter on
// its RGB16 (emulateReducedPresent), against the full-res route's output box-downsampled
// to the same dims. Thresholds pre-registered by the r3 design review (L3 0.85 retired:
// L3a >= 0.875 x the same route with notch and H-filter off, L3b >= 0.78 x ref). The
// frame without the notch and filter is printed for the record.
TEST(GpuDualIsoPreviewScale, ReducedReconWithNotchMatchesReference)
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
    bool lookPass = true;
    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
        int w = 0, h = 0;
        mlvFrameOutputDimensions(fixture.video(), scale, &w, &h);
        std::vector<uint8_t> routed(static_cast<size_t>(w) * h * 3);
        std::vector<uint16_t> raw = decodeRaw(fixture, 2);
        ASSERT_FALSE(raw.empty());
        std::vector<uint16_t> recon(static_cast<size_t>(plan.reducedWidth) * plan.reducedHeight);
        WorkerState worker;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(fixture.video(), &plan, raw.data(), recon.data(),
                                                    &worker.state, 1, nullptr, nullptr));
        std::vector<uint16_t> bayer(recon.size());
        dualiso_reduced_iso_period_notch16(bayer.data(), recon.data(),
                                           plan.reducedWidth, plan.reducedHeight);
        const int previousMode = processingPlaybackPreviewModeEnabled();
        const int previousScale = processingPlaybackPreviewScaleFactor();
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackPreviewScaleFactor(scale);
        const int ok = getMlvProcessedFrame8ScaledFromReducedReconnedRaw16(
            fixture.video(), 2, bayer.data(), plan.reducedWidth, plan.reducedHeight, plan.scale,
            routed.data(), 1, scale);
        ASSERT_EQ(1, ok);
        // The emulation without the filter is the routed frame, byte for byte.
        const std::vector<uint8_t> emulatedNoFilter =
            emulateReducedPresent(fixture, 2, bayer, plan.reducedWidth, plan.reducedHeight, h, false);
        const std::vector<uint8_t> reduced =
            emulateReducedPresent(fixture, 2, bayer, plan.reducedWidth, plan.reducedHeight, h, true);
        const std::vector<uint8_t> unfiltered =
            emulateReducedPresent(fixture, 2, recon, plan.reducedWidth, plan.reducedHeight, h, false);
        processingSetPlaybackPreviewScaleFactor(previousScale);
        processingSetPlaybackPreviewMode(previousMode);
        ASSERT_TRUE(emulatedNoFilter == routed);
        const int rh = plan.reducedHeight;
        const std::vector<uint8_t> ref = boxDownsample(fullFrame, fixture.width(), scale, w, rh);
        const double l3Off = lag4VerticalLumaDetail(unfiltered, w, rh);
        std::printf("[gpu-dualiso-preview-scale] x%d notch and H-filter off: L1 %.4f L2 %.4f L3 %.4f "
                    "L8 %.5f L9 %.4f\n",
                    scale, rowPeriod4Energy(unfiltered, w, rh), columnPeriod4Energy(unfiltered, w, rh),
                    l3Off, colNyqEnergy(unfiltered, w, rh), lag2HorizontalLumaDetail(unfiltered, w, rh));
        const double l1 = rowPeriod4Energy(reduced, w, rh);
        const double l1Ref = rowPeriod4Energy(ref, w, rh);
        const double l2 = columnPeriod4Energy(reduced, w, rh);
        const double l2Ref = columnPeriod4Energy(ref, w, rh);
        const double l3 = lag4VerticalLumaDetail(reduced, w, rh);
        const double l3Ref = lag4VerticalLumaDetail(ref, w, rh);
        const double l8 = colNyqEnergy(reduced, w, rh);
        const double l8Ref = colNyqEnergy(ref, w, rh);
        const double l9 = lag2HorizontalLumaDetail(reduced, w, rh);
        const double l9Ref = lag2HorizontalLumaDetail(ref, w, rh);
        std::printf("[gpu-dualiso-preview-scale] x%d L1 row-p4 %.4f (ref %.4f) L2 col-p4 %.4f "
                    "(ref %.4f, bound %.4f) L3 lag4 %.4f (ref %.4f, L3a %.4f >= 0.875, L3b %.4f >= 0.78) "
                    "L8 colNyq %.5f (ref %.5f, bound %.5f) L9 lag2 %.4f (ref %.4f, ratio %.4f >= 0.80)\n",
                    scale, l1, l1Ref, l2, l2Ref, 3.0 * l2Ref + 0.02, l3, l3Ref,
                    l3Off > 0.0 ? l3 / l3Off : 0.0, l3Ref > 0.0 ? l3 / l3Ref : 0.0,
                    l8, l8Ref, 2.0 * l8Ref + 0.002, l9, l9Ref, l9Ref > 0.0 ? l9 / l9Ref : 0.0);
        lookPass = lookPass && l1 <= 0.02;
        lookPass = lookPass && l2 <= 3.0 * l2Ref + 0.02;
        lookPass = lookPass && l3 >= 0.875 * l3Off;
        lookPass = lookPass && l3 >= 0.78 * l3Ref;
        lookPass = lookPass && l8 <= 2.0 * l8Ref + 0.002;
        lookPass = lookPass && l9 >= 0.80 * l9Ref;
        for (int c = 0; c < 3; ++c)
        {
            const double ratio = channelMean(reduced, w, rh, c) / channelMean(ref, w, rh, c);
            std::printf("[gpu-dualiso-preview-scale] x%d L4 channel %d mean ratio %.4f\n",
                        scale, c, ratio);
            lookPass = lookPass && std::fabs(ratio - 1.0) <= 0.02;
        }
    }
    ASSERT_TRUE(lookPass);
}

// T7 (r3): the reduced H-Nyquist filter's response at the 2-column period is exactly 0
// per channel, edges included (mirrored), it keeps each channel's mean within 1 LSB,
// and a step edge gets the exact [1,2,1]/4 values.
// Mutations: never applied, weights [1,0,1]/2, vertical instead of horizontal,
// applied twice, wrong edge mirroring, channel crosstalk.
TEST(GpuDualIsoPreviewScale, HNyquistNullsColumnAlternation)
{
    const int base[3] = { 12000, 20000, 9000 };
    const int amp[3] = { 600, 1000, 300 };
    for (const int w : { 8, 9, 2 })
    {
        const int h = 4;
        std::vector<uint16_t> src(static_cast<size_t>(w) * h * 3);
        for (int y = 0; y < h; ++y)
            for (int x = 0; x < w; ++x)
                for (int c = 0; c < 3; ++c)
                    src[(static_cast<size_t>(y) * w + x) * 3 + c] =
                        static_cast<uint16_t>(base[c] + ((x % 2 == 0) ? amp[c] : -amp[c]));
        std::vector<uint16_t> out(src.size(), 0u);
        debayer_reduced_hnyquist121_rgb16(out.data(), src.data(), w, h);
        for (int y = 0; y < h; ++y)
            for (int x = 0; x < w; ++x)
                for (int c = 0; c < 3; ++c)
                {
                    const uint16_t v = out[(static_cast<size_t>(y) * w + x) * 3 + c];
                    // Interior and mirrored edges: (l + 2c + r + 2) >> 2 of +-amp is the base.
                    if (v != base[c])
                        std::printf("[hnyquist] w=%d x=%d y=%d c=%d out=%u want=%d\n", w, x, y, c, v, base[c]);
                    ASSERT_EQ(base[c], static_cast<int>(v));
                }
    }

    // Step edge 4000 -> 8000 between x=3 and x=4 in channel G only, one row.
    const int w = 8;
    std::vector<uint16_t> step = rgb16Field(w, 1, 5000, 4000, 7000);
    for (int x = 4; x < w; ++x) step[static_cast<size_t>(x) * 3 + 1] = 8000;
    std::vector<uint16_t> out(step.size(), 0u);
    debayer_reduced_hnyquist121_rgb16(out.data(), step.data(), w, 1);
    const int wantG[8] = { 4000, 4000, 4000, 5000, 7000, 8000, 8000, 8000 };
    for (int x = 0; x < w; ++x)
    {
        ASSERT_EQ(wantG[x], static_cast<int>(out[static_cast<size_t>(x) * 3 + 1]));
        ASSERT_EQ(5000, static_cast<int>(out[static_cast<size_t>(x) * 3 + 0]));
        ASSERT_EQ(7000, static_cast<int>(out[static_cast<size_t>(x) * 3 + 2]));
    }
    // Edge mirroring is exact: x=0 takes (in[1] + 2 in[0] + in[1] + 2) >> 2.
    std::vector<uint16_t> edge = rgb16Field(4, 1, 0, 0, 0);
    edge[0 * 3 + 0] = 4000; // R at x=0
    edge[1 * 3 + 0] = 8000; // R at x=1
    debayer_reduced_hnyquist121_rgb16(out.data(), edge.data(), 4, 1);
    ASSERT_EQ(6000, static_cast<int>(out[0]));            // (8000 + 8000 + 8000 + 2) >> 2
    ASSERT_EQ(5000, static_cast<int>(out[1 * 3 + 0]));    // (4000 + 16000 + 0 + 2) >> 2
    ASSERT_EQ(2000, static_cast<int>(out[2 * 3 + 0]));    // (8000 + 0 + 0 + 2) >> 2
    ASSERT_EQ(0, static_cast<int>(out[3 * 3 + 0]));       // (0 + 0 + 0 + 2) >> 2, mirrored
}

// T8 (r3): rows and channels are independent: a signal on one row or one channel
// leaves every other row and channel untouched.
// Mutations: vertical instead of horizontal, channel crosstalk.
TEST(GpuDualIsoPreviewScale, HNyquistRowsAndChannelsIndependent)
{
    const int w = 10, h = 6;
    std::vector<uint16_t> src = rgb16Field(w, h, 10000, 20000, 30000);
    for (int x = 0; x < w; ++x)
        src[(static_cast<size_t>(3) * w + x) * 3 + 2] = static_cast<uint16_t>((x % 2) ? 40000 : 32000);
    src[(static_cast<size_t>(1) * w + 5) * 3 + 0] = 18000; // one R impulse on row 1
    std::vector<uint16_t> out(src.size(), 0u);
    debayer_reduced_hnyquist121_rgb16(out.data(), src.data(), w, h);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const size_t i = (static_cast<size_t>(y) * w + x) * 3;
            int wantR = 10000, wantB = 30000;
            if (y == 1 && x == 5) wantR = 14000;                  // (10000 + 36000 + 10000 + 2) >> 2
            else if (y == 1 && (x == 4 || x == 6)) wantR = 12000; // (10000 + 20000 + 18000 + 2) >> 2
            if (y == 3) wantB = 36000;                            // alternation nulled to its mean
            ASSERT_EQ(wantR, static_cast<int>(out[i + 0]));
            ASSERT_EQ(20000, static_cast<int>(out[i + 1]));
            ASSERT_EQ(wantB, static_cast<int>(out[i + 2]));
        }
}

// T9 (r3): a synthetic reduced Bayer16 band, 16 rows thick, whose texture aliases to the
// reduced grid's column Nyquist (bright and dark columns alternating inside the band, the
// shrink's uneven column sampling of a bright line), with a vertical edge for horizontal
// detail. Debayered by T3's debayerBasicU16 and by the CPU AMaZE, the band combs at the
// 2-column period; with the H-filter it meets L8 against the same band without the
// alias, and keeps L9. The comb without the filter is asserted too, so the band is a
// real RED case for the filter.
TEST(GpuDualIsoPreviewScale, ReducedBandCombMatchesReference)
{
    const int w = 96, h = 48;
    auto band = [&](int alias) {
        std::vector<uint16_t> bayer(static_cast<size_t>(w) * h);
        for (int y = 0; y < h; ++y)
            for (int x = 0; x < w; ++x)
            {
                const bool inBand = y >= 16 && y < 32;
                int v = 9000;
                if (inBand)
                {
                    v = (x < w / 2) ? 30000 : 22000; // a vertical edge inside the band
                    v += (x % 2 == 0) ? alias : -alias;
                }
                bayer[static_cast<size_t>(y) * w + x] = static_cast<uint16_t>(v);
            }
        return bayer;
    };
    const std::vector<uint16_t> aliased = band(9000);
    const std::vector<uint16_t> clean = band(0);
    auto debayer = [&](const std::vector<uint16_t> & bayer, bool amaze) {
        std::vector<uint16_t> rgb(static_cast<size_t>(w) * h * 3, 0u);
        if (amaze)
        {
            std::vector<float> f(bayer.begin(), bayer.end());
            ASSERT_EQ(1, debayerAmaze(rgb.data(), f.data(), w, h, 1, 0));
        }
        else
        {
            std::vector<uint16_t> work(bayer);
            debayerBasicU16(rgb.data(), work.data(), w, h, 1, 0);
        }
        return rgb;
    };
    auto rows = [&](const std::vector<uint8_t> & rgb8) {
        // The band interior, away from the debayer's band edges.
        return std::vector<uint8_t>(rgb8.begin() + static_cast<size_t>(19) * w * 3,
                                    rgb8.begin() + static_cast<size_t>(29) * w * 3);
    };
    for (const bool amaze : { false, true })
    {
        const std::vector<uint16_t> refRgb = debayer(clean, amaze);
        const std::vector<uint16_t> combRgb = debayer(aliased, amaze);
        std::vector<uint16_t> filtered(combRgb.size(), 0u);
        debayer_reduced_hnyquist121_rgb16(filtered.data(), combRgb.data(), w, h);
        const std::vector<uint8_t> ref = rows(rgb16To8(refRgb));
        const std::vector<uint8_t> comb = rows(rgb16To8(combRgb));
        const std::vector<uint8_t> out = rows(rgb16To8(filtered));
        const int bh = 10;
        const double l8Ref = colNyqEnergy(ref, w, bh);
        const double l8Comb = colNyqEnergy(comb, w, bh);
        const double l8 = colNyqEnergy(out, w, bh);
        const double l9Ref = lag2HorizontalLumaDetail(ref, w, bh);
        const double l9 = lag2HorizontalLumaDetail(out, w, bh);
        std::printf("[gpu-dualiso-preview-scale] T9 %s: L8 comb %.5f filtered %.5f (ref %.5f, bound %.5f) "
                    "L9 %.4f (ref %.4f, ratio %.4f)\n",
                    amaze ? "CPU AMaZE" : "debayerBasicU16", l8Comb, l8, l8Ref, 2.0 * l8Ref + 0.002,
                    l9, l9Ref, l9Ref > 0.0 ? l9 / l9Ref : 0.0);
        ASSERT_TRUE(l8Comb > 2.0 * l8Ref + 0.002); // the band combs without the filter
        ASSERT_TRUE(l8 <= 2.0 * l8Ref + 0.002);
        ASSERT_TRUE(l9 >= 0.80 * l9Ref);
    }
}

// c1 (opt-in, UM): MLVAPP_C1_HNYQUIST_DIR holds <case>/flag0.rgb16, flag1.rgb16 and
// dims.txt ("w h") per case, dumped by amaze_dll_test --reduced-hnyquist --dump-dir
// from the production AMaZE DLL. flag1 must equal debayer_reduced_hnyquist121_rgb16
// (flag0) at 0 LSB: the CUDA kernel against the real C reference.
TEST(GpuDualIsoPreviewScale, C1HnyquistDllParity)
{
    const char * dir = std::getenv("MLVAPP_C1_HNYQUIST_DIR");
    if (!dir || !*dir)
    {
        std::printf("[gpu-dualiso-preview-scale] c1 H-Nyquist skipped (MLVAPP_C1_HNYQUIST_DIR unset)\n");
        return;
    }
    const QDir root(QString::fromLocal8Bit(dir));
    const QStringList cases = root.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
    ASSERT_FALSE(cases.isEmpty());
    for (const QString & c : cases)
    {
        const std::string base = root.filePath(c).toStdString();
        int w = 0, h = 0;
        FILE * dims = std::fopen((base + "/dims.txt").c_str(), "r");
        ASSERT_TRUE(dims != nullptr);
        ASSERT_EQ(2, std::fscanf(dims, "%d %d", &w, &h));
        std::fclose(dims);
        const size_t words = static_cast<size_t>(w) * h * 3;
        std::vector<uint16_t> flag0(words), flag1(words), want(words);
        for (const auto & f : { std::make_pair(std::string("/flag0.rgb16"), &flag0),
                                std::make_pair(std::string("/flag1.rgb16"), &flag1) })
        {
            FILE * in = std::fopen((base + f.first).c_str(), "rb");
            ASSERT_TRUE(in != nullptr);
            ASSERT_EQ(words, std::fread(f.second->data(), sizeof(uint16_t), words, in));
            std::fclose(in);
        }
        debayer_reduced_hnyquist121_rgb16(want.data(), flag0.data(), w, h);
        size_t mismatches = 0;
        for (size_t i = 0; i < words; ++i) mismatches += want[i] != flag1[i];
        std::printf("[gpu-dualiso-preview-scale] c1 H-Nyquist %s %dx%d mismatches %zu / %zu\n",
                    c.toUtf8().constData(), w, h, mismatches, words);
        ASSERT_EQ(static_cast<size_t>(0), mismatches);
    }
}
// T4: the C seam sets the notch for reduced runs only. A reduced run sees 1; a
// full-res texture-route run (the x1 route), the CPU16 probe, the device and GL
// routes and an export render never run with it, even right after a reduced run
// (the backend flag is sticky). Mutations: notch never enabled; enabled on
// full-res runs; applied twice (the reduced-route test above compares bytes).
TEST(GpuDualIsoPreviewScale, ReducedIsoNotchOnlyOnReducedRuns)
{
    GpuReconEnv env(true);
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openGpuEligibleFixture(fixture));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    const FakeGpuBackendScope fake;
    int lastFlag = -1;
    uint64_t withNotch = 0, withoutNotch = 0, setCalls = 0;

    ASSERT_TRUE(gpuReducedFrame(fixture, 1, 4, nullptr));
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, &withoutNotch, &setCalls);
    ASSERT_EQ(1, lastFlag);
    ASSERT_EQ(1u, withNotch);
    ASSERT_TRUE(setCalls >= 1u);

    const int fullW = 16, fullH = 16;
    std::vector<uint16_t> full(static_cast<size_t>(fullW) * fullH, 100u);
    llrpGpuPlaybackReconState_t fullState = validatedState(7, fullW, fullH);
    llrpGpuPlaybackReconTiming_t timing = {};
    int rc = -1;
    llrpGpuPlaybackRetainedDeviceBayer16_t retained = {};
    ASSERT_NE(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&fullState, full.data(),
        full.size() * sizeof(uint16_t), &retained, &rc, &timing));
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, nullptr);
    ASSERT_EQ(0, lastFlag);
    for (size_t i = 0; i < full.size(); ++i) ASSERT_EQ(101u, retained.device_bayer16[i]);
    ASSERT_NE(0, llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(retained.token));

    ASSERT_TRUE(gpuReducedFrame(fixture, 1, 2, nullptr));
    std::vector<uint16_t> probeOut(full.size(), 0u);
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&fullState, full.data(),
        full.size() * sizeof(uint16_t), probeOut.data(), &rc, &timing));
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, nullptr);
    ASSERT_EQ(0, lastFlag);

    ASSERT_TRUE(gpuReducedFrame(fixture, 1, 4, nullptr));
    const uint16_t * device = nullptr;
    int dw = 0, dh = 0;
    ASSERT_NE(0, llrpGpuPlaybackReconRunDeviceBayer16(&fullState, full.data(),
        full.size() * sizeof(uint16_t), &device, &dw, &dh, &rc, &timing));
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, nullptr);
    ASSERT_EQ(0, lastFlag);

    ASSERT_TRUE(gpuReducedFrame(fixture, 1, 4, nullptr));
    ASSERT_NE(0, llrpGpuPlaybackReconRunGlTexture(&fullState, full.data(),
        full.size() * sizeof(uint16_t), 1u, &rc, &timing));
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, nullptr, nullptr);
    ASSERT_EQ(0, lastFlag);
    ASSERT_EQ(4u, withNotch);

    // An export render (CPU route, GPU export requested) never runs with the notch.
    GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
    GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_EXPORT", "1");
    ASSERT_FALSE(fixture.renderFrame16(1).empty());
    GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_EXPORT", "0");
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, nullptr, nullptr);
    ASSERT_EQ(4u, withNotch);
    ASSERT_EQ(0, llrpGpuPlaybackReconRetainedOutstandingCount());
}

// T5: a recon DLL without igpu_recon_set_reduced_iso_notch. The GPU plan refuses
// with the reason (the session latch then keeps scale 1, which the GUI policy
// table maps to the full-res texture route), and a reduced run is refused by the
// C seam, so no reduced frame is produced. Mutation: drop either refusal.
TEST(GpuDualIsoPreviewScale, MissingNotchSymbolRefusesTheReducedRoute)
{
    GpuReconEnv env(true);
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openGpuEligibleFixture(fixture));
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    const FakeGpuBackendScope fake;
    ASSERT_EQ(1, llrpGpuPlaybackReconReducedIsoNotchAvailable());
    mlvDualIsoPreviewScaleRecon_t admitted;
    ASSERT_EQ(1, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), 4, 1, &admitted));

    ASSERT_EQ(1, llrpSetFakeGpuBackendReducedIsoNotchSymbolForTesting(0));
    ASSERT_EQ(0, llrpGpuPlaybackReconReducedIsoNotchAvailable());
    for (const int scale : { 2, 4 })
    {
        for (const int latched : { 0, 1 })
        {
            mlvDualIsoPreviewScaleRecon_t plan;
            ASSERT_EQ(0, mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), scale, latched, &plan));
            ASSERT_EQ(1, plan.scale);
            ASSERT_EQ(std::string("recon DLL lacks the reduced ISO notch"), std::string(plan.reason));
        }
    }

    // The run path refuses too, with a plan admitted before the symbol went away.
    std::vector<uint16_t> raw = decodeRaw(fixture, 1);
    ASSERT_FALSE(raw.empty());
    std::vector<uint16_t> prepared(static_cast<size_t>(admitted.reducedWidth) * admitted.reducedHeight);
    WorkerState worker;
    const TextureRouteThreadOptIn optIn;
    mlv_pipeline_capture_set_current_frame(1);
    ASSERT_NE(1, mlvDualIsoGpuPreviewScaleReconRun(fixture.video(), &admitted, raw.data(),
                                                   prepared.data(), &worker.state, 1,
                                                   nullptr, nullptr));
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_NO_REDUCED_ISO_NOTCH, llrpGpuPlaybackReconLastRunRcForTesting());
    llrpGpuPlaybackRetainedDeviceBayer16_t device = {};
    ASSERT_TRUE(!llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(&device) || !device.valid);
    int lastFlag = -1;
    uint64_t withNotch = 0;
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, nullptr, nullptr);
    ASSERT_EQ(0u, withNotch);
    ASSERT_EQ(0, llrpGpuPlaybackReconRetainedOutstandingCount());
}
// c1 vectors (opt-in: MLVAPP_C1_VECTORS_DIR). For the large fixture's frame 2,
// writes tools/gpu/backend/dll_test.cpp vectors for the x2/x4 reduced CUDA route
// (in.u16, LUTs and scalars exactly as the fake backend received them; out.u16 =
// the CPU reduced recon plus the C reference notch) and for the full-res route
// (out.u16 = the CPU full recon), at playback hints 0 and 1. On a CUDA venue
// dll_test then replays the production DLL on them (--reduced-iso-notch for the
// reduced cases). Not a hosted-CI check: it skips without the variable.
TEST(GpuDualIsoPreviewScale, C1DumpDllParityVectors)
{
    const char * root = std::getenv("MLVAPP_C1_VECTORS_DIR");
    if (!root || !*root)
    {
        std::printf("[gpu-dualiso-preview-scale] c1 vectors skipped (MLVAPP_C1_VECTORS_DIR unset)\n");
        return;
    }
    GpuReconEnv env(true);
    MlvPipelineFixture fixture;
    QString error;
    ASSERT_TRUE(fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"), &error));
    ASSERT_TRUE(fixture.applyReceipt(&error));
    mlvObject_t * video = fixture.video();
    llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
    llrpSetDualIsoAliasMapMode(video, FR_ON);
    llrpSetDualIsoFullResBlendingMode(video, FR_ON);
    llrpSetChromaSmoothMode(video, CS_OFF);
    video->llrawproc->focus_pixels = 0;
    video->llrawproc->bad_pixels = 0;
    video->llrawproc->vertical_stripes = 0;
    ASSERT_FALSE(fixture.renderFrame8(0).empty());
    const FakeGpuBackendScope fake;
    auto writeBlob = [](const std::string & path, const std::vector<uint16_t> & v) {
        FILE * f = std::fopen(path.c_str(), "wb");
        if (!f) return false;
        const bool ok = std::fwrite(v.data(), sizeof(uint16_t), v.size(), f) == v.size();
        std::fclose(f);
        return ok;
    };
    const int previousMode = processingPlaybackPreviewModeEnabled();
    const int previousScale = processingPlaybackPreviewScaleFactor();
    const int previousActive = video->playback_scale_factor_active;
    for (const int hint : { 0, 1 })
    {
        processingSetPlaybackPreviewMode(hint);
        video->playback_scale_factor_active = hint ? 1 : previousActive;
        for (const int scale : { 2, 4 })
        {
            const std::string dir = std::string(root) + "/x" + std::to_string(scale)
                                  + "-hint" + std::to_string(hint);
            QDir().mkpath(QString::fromStdString(dir));
            processingSetPlaybackPreviewScaleFactor(scale);
            GPU_DUALISO_TEST_SETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR", dir.c_str());
            GpuReducedFrame gpu;
            const bool ran = gpuReducedFrame(fixture, 2, scale, &gpu);
            GPU_DUALISO_TEST_UNSETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR");
            ASSERT_TRUE(ran);

            GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
            mlvDualIsoPreviewScaleRecon_t plan;
            ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(video, scale, &plan));
            std::vector<uint16_t> raw = decodeRaw(fixture, 2);
            ASSERT_FALSE(raw.empty());
            std::vector<uint16_t> recon(static_cast<size_t>(plan.reducedWidth) * plan.reducedHeight);
            WorkerState worker;
            ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(video, &plan, raw.data(), recon.data(),
                                                        &worker.state, 1, nullptr, nullptr));
            GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
            std::vector<uint16_t> notched(recon.size());
            dualiso_reduced_iso_period_notch16(notched.data(), recon.data(),
                                               plan.reducedWidth, plan.reducedHeight);
            ASSERT_TRUE(writeBlob(dir + "/out.u16", notched));
            ASSERT_TRUE(writeBlob(dir + "/out_without_notch.u16", recon));
            std::printf("[gpu-dualiso-preview-scale] c1 vectors %s %dx%d\n", dir.c_str(),
                        plan.reducedWidth, plan.reducedHeight);
        }

        // Full resolution: the texture route's prepare-only run, then the CPU recon.
        const std::string dir = std::string(root) + "/x1-hint" + std::to_string(hint);
        QDir().mkpath(QString::fromStdString(dir));
        processingSetPlaybackPreviewScaleFactor(1);
        std::vector<uint16_t> gpuRaw = decodeRaw(fixture, 2);
        ASSERT_FALSE(gpuRaw.empty());
        {
            WorkerState worker;
            const TextureRouteThreadOptIn optIn;
            mlv_pipeline_capture_set_current_frame(2);
            GPU_DUALISO_TEST_SETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR", dir.c_str());
            applyLLRawProcObjectWorker(video, gpuRaw.data(), gpuRaw.size() * sizeof(uint16_t),
                                       &worker.state, 0);
            GPU_DUALISO_TEST_UNSETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR");
            llrpGpuPlaybackRetainedDeviceBayer16_t device = {};
            if (llrpGpuPlaybackReconGetLastRetainedDeviceBayer16(&device) && device.valid)
                llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(device.token);
        }
        GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
        std::vector<uint16_t> cpuRaw = decodeRaw(fixture, 2);
        {
            WorkerState worker;
            applyLLRawProcObjectWorker(video, cpuRaw.data(), cpuRaw.size() * sizeof(uint16_t),
                                       &worker.state, 0);
        }
        GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
        ASSERT_TRUE(writeBlob(dir + "/out.u16", cpuRaw));
        std::printf("[gpu-dualiso-preview-scale] c1 vectors %s %dx%d\n", dir.c_str(),
                    fixture.width(), fixture.height());
    }
    processingSetPlaybackPreviewScaleFactor(previousScale);
    processingSetPlaybackPreviewMode(previousMode);
    video->playback_scale_factor_active = previousActive;
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
