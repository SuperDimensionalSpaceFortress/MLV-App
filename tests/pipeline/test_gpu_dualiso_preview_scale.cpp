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
// r4: the comb's source is the shrink. The Phase 4B decimators sample off-site (x4 G
// -0.875 px in columns, rows -0.375..-2.625 px), so Gr and Gb disagree on any vertical
// brightness change and AMaZE renders that as a 2-column comb. The GPU plan now shrinks
// with pl_downsample_bayer_to_bayer_phase_tent (same-colour, same-ISO-row tent bins on the
// correct sites; T11-T13), the CPU plan keeps its decimators byte for byte, and the r3
// filter is off (kReducedHnyquistOnReducedPresents = 0; T7-T9 keep its C reference). T3
// grades the route (CPU AMaZE, as at the venue) against ref_bin, the full-res recon
// tent-binned to the reduced size; the box reference's figures are reported only.
// The pixel parity of the real CUDA kernels at reduced dims ((c1), (c2)) is a venue
// proof; see the PR.
#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/llrawproc/dualiso.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/debayer/debayer.h"
#include "../../src/processing/playback_downsample.h"
#include "../../platform/qt/PlaybackScaling.h"

#include <QDir>

#include <algorithm>
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
// this frame (it syncs the processing levels); with hnyquist=false and amaze=false the
// output equals it. r4: amaze=true debayers with the CPU AMaZE, as the venue route does.
std::vector<uint8_t> emulateReducedPresent(MlvPipelineFixture & fixture,
                                           uint64_t frame,
                                           const std::vector<uint16_t> & bayer,
                                           int reducedWidth,
                                           int reducedHeight,
                                           int outHeight,
                                           bool hnyquist,
                                           bool amaze = false)
{
    const size_t rowWords = static_cast<size_t>(reducedWidth) * 3u;
    std::vector<uint16_t> work(bayer);
    std::vector<uint16_t> rgb(rowWords * static_cast<size_t>(outHeight), 0u);
    if (amaze)
    {
        std::vector<float> f(work.begin(), work.end());
        if (debayerAmaze(rgb.data(), f.data(), reducedWidth, reducedHeight, 1, 0) != 1) return {};
    }
    else
    {
        debayerBasicU16(rgb.data(), work.data(), reducedWidth, reducedHeight, 1, 0);
    }
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

// r4 ref_bin's shrink: a same-colour tent bin of a full-res (reconstructed, so no ISO
// rows) Bayer16, rows and columns both by class mod 2, centred on t = S*o + (S-1)/2 with
// radius S; an out-of-range tap reads the nearest in-range sample of its class.
std::vector<uint16_t> sameColourTentBin(const std::vector<uint16_t> & full, int fullW, int fullH,
                                        int scale, int outW, int outH)
{
    auto taps = [scale](int o, int limit) {
        std::vector<std::pair<int, double>> t;
        const double target = scale * o + (scale - 1) / 2.0;
        for (int s = static_cast<int>(target) - scale - 2; s <= static_cast<int>(target) + scale + 2; ++s)
        {
            if (((s % 2) + 2) % 2 != o % 2) continue;
            const double d = std::fabs(s - target);
            if (d >= scale) continue;
            int c = s;
            while (c < 0) c += 2;
            while (c >= limit) c -= 2;
            t.push_back({ c, scale - d });
        }
        return t;
    };
    std::vector<uint16_t> out(static_cast<size_t>(outW) * outH);
    for (int y = 0; y < outH; ++y)
    {
        const auto ry = taps(y, fullH);
        for (int x = 0; x < outW; ++x)
        {
            const auto rx = taps(x, fullW);
            double sum = 0.0, wsum = 0.0;
            for (const auto & ty : ry)
                for (const auto & tx : rx)
                {
                    sum += ty.second * tx.second
                         * full[static_cast<size_t>(ty.first) * fullW + static_cast<size_t>(tx.first)];
                    wsum += ty.second * tx.second;
                }
            out[static_cast<size_t>(y) * outW + x] = static_cast<uint16_t>(std::lround(sum / wsum));
        }
    }
    return out;
}

// The playback hint the reduced route reconstructs with (x1; see the route test above).
struct X1PlaybackHint
{
    explicit X1PlaybackHint(mlvObject_t * v)
        : video(v), mode(processingPlaybackPreviewModeEnabled()),
          scale(processingPlaybackPreviewScaleFactor()), active(v->playback_scale_factor_active)
    {
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackPreviewScaleFactor(1);
        video->playback_scale_factor_active = 1;
    }
    ~X1PlaybackHint()
    {
        processingSetPlaybackPreviewScaleFactor(scale);
        processingSetPlaybackPreviewMode(mode);
        video->playback_scale_factor_active = active;
    }
    mlvObject_t * video;
    int mode, scale, active;
};
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

// T3 (r4 gates, pre-registered by the r4 design review). E, the route emulation: the CPU
// reduced recon (CUDA matches it, c1) on the phase-tent shrink, plus the C reference notch,
// debayered by the CPU AMaZE (as the venue's CUDA AMaZE does) and presented through the
// reduced consumer's processing, with the C reference H-Nyquist filter on its RGB16 iff
// kReducedHnyquistOnReducedPresents is 1. ref_bin, the ideal reduced preview: the full-res
// CPU recon (x1 hint) tent-binned by colour (sameColourTentBin), then the same AMaZE and
// processing. ref_box: the full-res route's output box-downsampled (the r1b-r3 reference).
// Gated: L1 row-p4 <= 0.02; L2 col-p4 <= 3 x ref_box + 0.02; L4 channel means within 0.02
// of ref_bin; L8 colNyq <= 2 x ref_bin + 0.002. L3r (lag-4 / ref_bin, was >= 0.85) and L9r
// (lag-2 / ref_bin, was >= 0.90) are report-only by the r6 design ruling, gated only by the
// gross-blur tripwire L3r, L9r >= 0.55. Reported only: L3a, L3b and L9 against ref_box, the same metrics
// through debayerBasicU16, the old decimator's route, and M-L6 (ref_bin / ref_box channel
// means, the Jensen account of the venue's +3-4 % R).
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
    mlvObject_t * video = fixture.video();

    // The full-res CPU recon at the x1 hint, for ref_bin.
    std::vector<uint16_t> fullRecon = decodeRaw(fixture, 2);
    ASSERT_FALSE(fullRecon.empty());
    {
        const X1PlaybackHint hint(video);
        WorkerState worker;
        applyLLRawProcObjectWorker(video, fullRecon.data(), fullRecon.size() * sizeof(uint16_t),
                                   &worker.state, 0);
    }

    bool lookPass = true;
    for (const int scale : { 2, 4 })
    {
        mlvDualIsoPreviewScaleRecon_t plan;
        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(video, scale, &plan));
        ASSERT_EQ(0, plan.phaseTentShrink);
        mlvDualIsoPreviewScaleRecon_t tentPlan = plan;
        tentPlan.phaseTentShrink = 1;
        int w = 0, h = 0;
        mlvFrameOutputDimensions(video, scale, &w, &h);
        const int rw = plan.reducedWidth;
        const int rh = plan.reducedHeight;

        // The CPU plan's shrink is the Phase 4B decimator, byte for byte.
        {
            std::vector<uint16_t> raw = decodeRaw(fixture, 2);
            std::vector<uint16_t> viaPlan(static_cast<size_t>(rw) * rh), direct(viaPlan.size());
            WorkerState worker;
            const int shrunk = mlvDualIsoPreviewScaleReconShrink(video, &plan, raw.data(), viaPlan.data(),
                                                                 &worker.state, 1, nullptr, nullptr);
            ASSERT_TRUE(shrunk == 1 || shrunk == 2); // 2: the full-res fixes ran on raw in place
            int ow = 0, oh = 0;
            ASSERT_EQ(0, scale == 2
                ? pl_downsample_bayer_to_bayer_2x(raw.data(), fixture.width(), plan.sourceHeight,
                                                  direct.data(), &ow, &oh, 1)
                : pl_downsample_bayer_to_bayer_4x(raw.data(), fixture.width(), plan.sourceHeight,
                                                  direct.data(), &ow, &oh, 1));
            ASSERT_TRUE(viaPlan == direct);
        }

        std::vector<uint16_t> tentRecon(static_cast<size_t>(rw) * rh), oldRecon(tentRecon.size());
        {
            const X1PlaybackHint hint(video);
            for (int pass = 0; pass < 2; ++pass)
            {
                std::vector<uint16_t> raw = decodeRaw(fixture, 2);
                ASSERT_FALSE(raw.empty());
                WorkerState worker;
                ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(video, pass ? &plan : &tentPlan, raw.data(),
                                                            pass ? oldRecon.data() : tentRecon.data(),
                                                            &worker.state, 1, nullptr, nullptr));
            }
        }
        std::vector<uint16_t> bayer(tentRecon.size()), oldBayer(oldRecon.size());
        dualiso_reduced_iso_period_notch16(bayer.data(), tentRecon.data(), rw, rh);
        dualiso_reduced_iso_period_notch16(oldBayer.data(), oldRecon.data(), rw, rh);
        const std::vector<uint16_t> binned =
            sameColourTentBin(fullRecon, fixture.width(), fixture.height(), scale, rw, rh);

        const bool hnyquist = playbackReducedHnyquistWanted(true);
        std::vector<uint8_t> routed(static_cast<size_t>(w) * h * 3);
        const int previousMode = processingPlaybackPreviewModeEnabled();
        const int previousScale = processingPlaybackPreviewScaleFactor();
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackPreviewScaleFactor(scale);
        const int ok = getMlvProcessedFrame8ScaledFromReducedReconnedRaw16(
            video, 2, bayer.data(), rw, rh, plan.scale, routed.data(), 1, scale);
        // The emulation without the filter, through debayerBasicU16, is the routed frame.
        const std::vector<uint8_t> emulatedBasic =
            emulateReducedPresent(fixture, 2, bayer, rw, rh, h, false);
        const std::vector<uint8_t> e = emulateReducedPresent(fixture, 2, bayer, rw, rh, h, hnyquist, true);
        const std::vector<uint8_t> refBin = emulateReducedPresent(fixture, 2, binned, rw, rh, h, false, true);
        const std::vector<uint8_t> unnotched =
            emulateReducedPresent(fixture, 2, tentRecon, rw, rh, h, false, true);
        const std::vector<uint8_t> oldRoute = emulateReducedPresent(fixture, 2, oldBayer, rw, rh, h, false, true);
        const std::vector<uint8_t> eBasic = emulateReducedPresent(fixture, 2, bayer, rw, rh, h, hnyquist);
        const std::vector<uint8_t> refBinBasic = emulateReducedPresent(fixture, 2, binned, rw, rh, h, false);
        processingSetPlaybackPreviewScaleFactor(previousScale);
        processingSetPlaybackPreviewMode(previousMode);
        ASSERT_EQ(1, ok);
        ASSERT_TRUE(emulatedBasic == routed);
        ASSERT_FALSE(e.empty());
        ASSERT_FALSE(refBin.empty());
        ASSERT_FALSE(unnotched.empty());
        ASSERT_FALSE(oldRoute.empty());
        const std::vector<uint8_t> refBox = boxDownsample(fullFrame, fixture.width(), scale, w, rh);

        auto report = [&](const char * label, const std::vector<uint8_t> & img, const std::vector<uint8_t> & rb) {
            std::printf("[gpu-dualiso-preview-scale] x%d %s: L1 %.4f L2 %.4f L3 %.4f (/ref_bin %.4f, /ref_box %.4f) "
                        "L8 %.5f L9 %.4f (/ref_bin %.4f, /ref_box %.4f)\n",
                        scale, label, rowPeriod4Energy(img, w, rh), columnPeriod4Energy(img, w, rh),
                        lag4VerticalLumaDetail(img, w, rh),
                        lag4VerticalLumaDetail(img, w, rh) / lag4VerticalLumaDetail(rb, w, rh),
                        lag4VerticalLumaDetail(img, w, rh) / lag4VerticalLumaDetail(refBox, w, rh),
                        colNyqEnergy(img, w, rh), lag2HorizontalLumaDetail(img, w, rh),
                        lag2HorizontalLumaDetail(img, w, rh) / lag2HorizontalLumaDetail(rb, w, rh),
                        lag2HorizontalLumaDetail(img, w, rh) / lag2HorizontalLumaDetail(refBox, w, rh));
        };
        report("ref_bin (AMaZE)", refBin, refBin);
        report("ref_box", refBox, refBin);
        report("tent, notch off (AMaZE)", unnotched, refBin);
        report("old decimator + notch (AMaZE, r2/r3 route unfiltered)", oldRoute, refBin);
        report("E through debayerBasicU16 (report only; vs ref_bin basic)", eBasic, refBinBasic);
        report("ref_bin through debayerBasicU16", refBinBasic, refBinBasic);

        const double l1 = rowPeriod4Energy(e, w, rh);
        const double l2 = columnPeriod4Energy(e, w, rh);
        const double l2Box = columnPeriod4Energy(refBox, w, rh);
        const double l3 = lag4VerticalLumaDetail(e, w, rh);
        const double l3Bin = lag4VerticalLumaDetail(refBin, w, rh);
        const double l3Box = lag4VerticalLumaDetail(refBox, w, rh);
        const double l3Off = lag4VerticalLumaDetail(unnotched, w, rh);
        const double l8 = colNyqEnergy(e, w, rh);
        const double l8Bin = colNyqEnergy(refBin, w, rh);
        const double l9 = lag2HorizontalLumaDetail(e, w, rh);
        const double l9Bin = lag2HorizontalLumaDetail(refBin, w, rh);
        const double l9Box = lag2HorizontalLumaDetail(refBox, w, rh);
        std::printf("[gpu-dualiso-preview-scale] x%d E (hnyquist %d): L1 row-p4 %.4f <= 0.02 | L2 col-p4 %.4f "
                    "(ref_box %.4f, bound %.4f) | L3r %.4f (report; tripwire >= 0.55) (lag4 %.4f, ref_bin %.4f) | "
                    "L9r %.4f (report; tripwire >= 0.55) (lag2 %.4f, ref_bin %.4f) | L8 colNyq %.5f (ref_bin %.5f, "
                    "bound %.5f) | report: L3a %.4f "
                    "L3b %.4f L9 %.4f (ref_box lag4 %.4f lag2 %.4f)\n",
                    scale, hnyquist ? 1 : 0, l1, l2, l2Box, 3.0 * l2Box + 0.02, l3 / l3Bin, l3, l3Bin,
                    l9 / l9Bin, l9, l9Bin, l8, l8Bin, 2.0 * l8Bin + 0.002, l3Off > 0.0 ? l3 / l3Off : 0.0,
                    l3 / l3Box, l9 / l9Box, l3Box, l9Box);
        lookPass = lookPass && l1 <= 0.02;
        lookPass = lookPass && l2 <= 3.0 * l2Box + 0.02;
        lookPass = lookPass && l3 >= 0.55 * l3Bin;
        lookPass = lookPass && l9 >= 0.55 * l9Bin;
        lookPass = lookPass && l8 <= 2.0 * l8Bin + 0.002;
        for (int c = 0; c < 3; ++c)
        {
            const double ratio = channelMean(e, w, rh, c) / channelMean(refBin, w, rh, c);
            const double ml6 = channelMean(refBin, w, rh, c) / channelMean(refBox, w, rh, c);
            const double vsBox = channelMean(e, w, rh, c) / channelMean(refBox, w, rh, c);
            std::printf("[gpu-dualiso-preview-scale] x%d L4 channel %d E/ref_bin %.4f (within 0.02) | "
                        "report: E/ref_box %.4f, M-L6 ref_bin/ref_box %.4f\n", scale, c, ratio, vsBox, ml6);
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

// T11 (r4): pl_downsample_bayer_to_bayer_phase_tent puts every output on its own site.
// A per-plane linear ramp in x and y comes out exactly as the ramp at each target centre
// t = S*o + (S-1)/2 (x2 and x4, all four ISO row classes, both colour columns); a field
// constant per (ISO row class, colour column) comes out as the same constants, edges
// included; a signal in one ISO row class or one colour plane stays there; and the weight
// tables of the r4 design are exact.
// Mutations: phase off by one source column, rows grouped by class mod 2, weights not
// normalised, edge clamp crossing class.
TEST(GpuDualIsoPreviewScale, PhaseTentSitesAndClasses)
{
    const int inW = 64, inH = 64;
    auto shrink = [&](const std::vector<uint16_t> & in, int scale) {
        std::vector<uint16_t> out(static_cast<size_t>(inW / scale) * (inH / scale), 0u);
        int ow = 0, oh = 0;
        if (pl_downsample_bayer_to_bayer_phase_tent(in.data(), inW, inH, out.data(), scale, &ow, &oh, 1) != 0
            || ow != inW / scale || oh != inH / scale)
            out.clear();
        return out;
    };
    auto field = [&](auto value) {
        std::vector<uint16_t> f(static_cast<size_t>(inW) * inH);
        for (int y = 0; y < inH; ++y)
            for (int x = 0; x < inW; ++x) f[static_cast<size_t>(y) * inW + x] = static_cast<uint16_t>(value(x, y));
        return f;
    };
    auto base = [](int x, int y) { return 1000 + 500 * ((y % 4) * 2 + (x % 2)); };
    for (const int scale : { 2, 4 })
    {
        const int ow = inW / scale, oh = inH / scale;
        // Ramp: 2*x + 4*y on per-plane bases; 2*t_x and 4*t_y are integers.
        const std::vector<uint16_t> ramp = shrink(field([&](int x, int y) { return base(x, y) + 2 * x + 4 * y; }), scale);
        ASSERT_FALSE(ramp.empty());
        for (int y = 1; y < oh - 1; ++y)
            for (int x = 1; x < ow - 1; ++x)
            {
                const int want = base(x, y) + (2 * scale * x + scale - 1) + 2 * (2 * scale * y + scale - 1);
                const int got = ramp[static_cast<size_t>(y) * ow + x];
                if (got != want) std::printf("[phase-tent] x%d ramp (%d,%d) got %d want %d\n", scale, x, y, got, want);
                ASSERT_EQ(want, got);
            }
        // Constant per class, edges included.
        const std::vector<uint16_t> classes = shrink(field(base), scale);
        ASSERT_FALSE(classes.empty());
        for (int y = 0; y < oh; ++y)
            for (int x = 0; x < ow; ++x) ASSERT_EQ(base(x, y), static_cast<int>(classes[static_cast<size_t>(y) * ow + x]));
        const std::vector<uint16_t> flat = shrink(field([](int, int) { return 12345; }), scale);
        for (const uint16_t v : flat) ASSERT_EQ(12345, static_cast<int>(v));
        // One ISO row class, then one colour plane.
        for (int r = 0; r < 4; ++r)
        {
            const std::vector<uint16_t> out = shrink(field([r](int, int y) { return (y % 4 == r) ? 4000 : 0; }), scale);
            for (int y = 0; y < oh; ++y)
                for (int x = 0; x < ow; ++x)
                    ASSERT_EQ((y % 4 == r) ? 4000 : 0, static_cast<int>(out[static_cast<size_t>(y) * ow + x]));
        }
        for (int p = 0; p < 2; ++p)
        {
            const std::vector<uint16_t> out = shrink(field([p](int x, int) { return (x % 2 == p) ? 4000 : 0; }), scale);
            for (int y = 0; y < oh; ++y)
                for (int x = 0; x < ow; ++x)
                    ASSERT_EQ((x % 2 == p) ? 4000 : 0, static_cast<int>(out[static_cast<size_t>(y) * ow + x]));
        }
    }

    // The weight tables: an impulse column (row) of 64000 gives out = 64000 * w / colsum (rowsum).
    struct Table { int scale; int outIndex; int src[4]; int w[4]; int n; };
    const Table cols[] = {
        { 4, 2, { 6, 8, 10, 12 }, { 1, 5, 7, 3 }, 4 },   // even out col 2k (k=1): 8k-2, 8k, 8k+2, 8k+4
        { 4, 3, { 11, 13, 15, 17 }, { 3, 7, 5, 1 }, 4 }, // odd out col 2k+1: 8k+3, +5, +7, +9
        { 2, 2, { 4, 6 }, { 3, 1 }, 2 },                 // even col: 4k, 4k+2
        { 2, 3, { 5, 7 }, { 1, 3 }, 2 },                 // odd col: 4k+1, 4k+3
    };
    for (const Table & t : cols)
    {
        const int colsum = t.scale * t.scale;
        const int ow = inW / t.scale;
        int total = 0;
        for (int i = 0; i < t.n; ++i)
        {
            const int s = t.src[i];
            const std::vector<uint16_t> out = shrink(field([s](int x, int) { return x == s ? 64000 : 0; }), t.scale);
            ASSERT_EQ(64000 / colsum * t.w[i], static_cast<int>(out[static_cast<size_t>(5) * ow + t.outIndex]));
            total += t.w[i];
        }
        ASSERT_EQ(colsum, total);
    }
    const Table rows[] = {
        { 4, 4, { 12, 16, 20, 24 }, { 5, 13, 11, 3 }, 4 },  // r=0, j=1: 16j-4, 16j, +4, +8
        { 4, 5, { 17, 21, 25, 29 }, { 7, 15, 9, 1 }, 4 },   // r=1: 16j+1, +5, +9, +13
        { 4, 6, { 18, 22, 26, 30 }, { 1, 9, 15, 7 }, 4 },   // r=2: 16j+2, +6, +10, +14
        { 4, 7, { 23, 27, 31, 35 }, { 3, 11, 13, 5 }, 4 },  // r=3: 16j+7, +11, +15, +19
        { 2, 4, { 8, 12 }, { 7, 1 }, 2 },                   // r=0, j=1: 8j, 8j+4
        { 2, 5, { 9, 13 }, { 5, 3 }, 2 },                   // r=1: 8j+1, 8j+5
        { 2, 6, { 10, 14 }, { 3, 5 }, 2 },                  // r=2: 8j+2, 8j+6
        { 2, 7, { 11, 15 }, { 1, 7 }, 2 },                  // r=3: 8j+3, 8j+7
    };
    for (const Table & t : rows)
    {
        const int rowsum = 2 * t.scale * t.scale;
        const int ow = inW / t.scale;
        int total = 0;
        for (int i = 0; i < t.n; ++i)
        {
            const int s = t.src[i];
            const std::vector<uint16_t> out = shrink(field([s](int, int y) { return y == s ? 64000 : 0; }), t.scale);
            ASSERT_EQ(64000 / rowsum * t.w[i], static_cast<int>(out[static_cast<size_t>(t.outIndex) * ow + 5]));
            total += t.w[i];
        }
        ASSERT_EQ(rowsum, total);
    }

    // Same dims rules as the decimators; scale 2 or 4 only.
    std::vector<uint16_t> in(static_cast<size_t>(64) * 72, 0u), out(in.size(), 0u);
    int ow = 0, oh = 0;
    ASSERT_NE(0, pl_downsample_bayer_to_bayer_phase_tent(in.data(), 64, 72, out.data(), 4, &ow, &oh, 1));
    ASSERT_EQ(0, pl_downsample_bayer_to_bayer_phase_tent(in.data(), 64, 72, out.data(), 2, &ow, &oh, 1));
    ASSERT_NE(0, pl_downsample_bayer_to_bayer_phase_tent(in.data(), 62, 64, out.data(), 2, &ow, &oh, 1));
    ASSERT_NE(0, pl_downsample_bayer_to_bayer_phase_tent(in.data(), 64, 64, out.data(), 8, &ow, &oh, 1));
    // Threaded output equals single-threaded.
    const std::vector<uint16_t> noisy = field([](int x, int y) { return (x * 7919 + y * 104729) % 60000; });
    for (const int scale : { 2, 4 })
    {
        std::vector<uint16_t> one(noisy.size() / (scale * scale)), many(one.size());
        ASSERT_EQ(0, pl_downsample_bayer_to_bayer_phase_tent(noisy.data(), inW, inH, one.data(), scale, &ow, &oh, 1));
        ASSERT_EQ(0, pl_downsample_bayer_to_bayer_phase_tent(noisy.data(), inW, inH, many.data(), scale, &ow, &oh, 4));
        ASSERT_TRUE(one == many);
    }
}

// T11b (r4): only the GPU plan shrinks with the phase tent, at x2 and x4; the CPU plan's
// shrink stays the Phase 4B decimator byte for byte; each tent shrink is counted for the
// play-stop summary. Mutations: tent enabled on the CPU plan, old decimator kept for x2.
TEST(GpuDualIsoPreviewScale, PhaseTentShrinkOnlyOnTheGpuPlan)
{
    for (const bool gpu : { true, false })
    {
        GpuReconEnv env(gpu);
        MlvPipelineFixture fixture;
        ASSERT_TRUE(openGpuEligibleFixture(fixture));
        ASSERT_FALSE(fixture.renderFrame8(0).empty());
        for (const int scale : { 2, 4 })
        {
            mlvDualIsoPreviewScaleRecon_t plan;
            ASSERT_EQ(1, gpu ? mlvDualIsoGpuPreviewScaleReconPlan(fixture.video(), scale, 1, &plan)
                             : mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
            ASSERT_EQ(gpu ? 1 : 0, plan.phaseTentShrink);
            std::vector<uint16_t> raw = decodeRaw(fixture, 1);
            ASSERT_FALSE(raw.empty());
            std::vector<uint16_t> viaPlan(static_cast<size_t>(plan.reducedWidth) * plan.reducedHeight, 0u);
            std::vector<uint16_t> direct(viaPlan.size(), 1u);
            const uint64_t framesBefore = mlvDualIsoPhaseTentShrinkFrames();
            WorkerState worker;
            ASSERT_EQ(1, mlvDualIsoPreviewScaleReconShrink(fixture.video(), &plan, raw.data(), viaPlan.data(),
                                                           &worker.state, 1, nullptr, nullptr));
            int ow = 0, oh = 0;
            if (gpu)
                ASSERT_EQ(0, pl_downsample_bayer_to_bayer_phase_tent(raw.data(), fixture.width(), plan.sourceHeight,
                                                                     direct.data(), scale, &ow, &oh, 1));
            else if (scale == 2)
                ASSERT_EQ(0, pl_downsample_bayer_to_bayer_2x(raw.data(), fixture.width(), plan.sourceHeight,
                                                             direct.data(), &ow, &oh, 1));
            else
                ASSERT_EQ(0, pl_downsample_bayer_to_bayer_4x(raw.data(), fixture.width(), plan.sourceHeight,
                                                             direct.data(), &ow, &oh, 1));
            ASSERT_TRUE(viaPlan == direct);
            ASSERT_EQ(framesBefore + (gpu ? 1u : 0u), mlvDualIsoPhaseTentShrinkFrames());
        }
    }
}

namespace {
// RGGB Bayer16 from a per-row brightness profile (G level), R and B at fixed fractions.
std::vector<uint16_t> bayerFromRowProfile(int w, int h, const std::vector<double> & g)
{
    std::vector<uint16_t> bayer(static_cast<size_t>(w) * h);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const bool rRow = (y % 2) == 0, evenCol = (x % 2) == 0;
            const double k = (rRow && evenCol) ? 0.55 : (!rRow && !evenCol) ? 0.45 : 1.0;
            bayer[static_cast<size_t>(y) * w + x] = static_cast<uint16_t>(std::lround(g[static_cast<size_t>(y)] * k));
        }
    return bayer;
}

std::vector<uint16_t> oldDecimator(const std::vector<uint16_t> & in, int w, int h, int scale)
{
    std::vector<uint16_t> out(static_cast<size_t>(w / scale) * (h / scale), 0u);
    int ow = 0, oh = 0;
    const int rc = scale == 2 ? pl_downsample_bayer_to_bayer_2x(in.data(), w, h, out.data(), &ow, &oh, 1)
                              : pl_downsample_bayer_to_bayer_4x(in.data(), w, h, out.data(), &ow, &oh, 1);
    if (rc != 0) out.clear();
    return out;
}

std::vector<uint16_t> phaseTent(const std::vector<uint16_t> & in, int w, int h, int scale)
{
    std::vector<uint16_t> out(static_cast<size_t>(w / scale) * (h / scale), 0u);
    int ow = 0, oh = 0;
    if (pl_downsample_bayer_to_bayer_phase_tent(in.data(), w, h, out.data(), scale, &ow, &oh, 1) != 0) out.clear();
    return out;
}
} // namespace

// T12 (r4): the comb from the old shrink's mis-phase. A synthetic full-res Bayer16 with a
// horizontal bright band 16 reduced rows thick (16 S source rows) whose edges are vertical
// gradients 4 reduced rows long, shrunk by the OLD decimator and by the phase tent, then
// debayered by the CPU AMaZE at the reduced size. Over the band and its edges the tent
// stays at colNyq <= 0.002. The pre-registered RED case (old > 0.002) did NOT hold: on an
// x-invariant band the old shrink's Gr/Gb mismatch reads 0.00000 through AMaZE (it
// interpolates G along the row where the vertical gradient is strong), so the comb proof
// rests on venue L8 and L7. A band tilted by one source row per 8 columns is printed for
// the record (not gated).
TEST(GpuDualIsoPreviewScale, ReducedBandCombFromMisphase)
{
    for (const int scale : { 2, 4 })
    for (const int tilt : { 0, 8 })
    {
        const int rw = 96, rhh = 64;
        const int w = rw * scale, h = rhh * scale;
        const double floorLevel = 8000.0, bandLevel = 40000.0;
        const double rampLen = 4.0 * scale;
        std::vector<uint16_t> full(static_cast<size_t>(w) * h);
        for (int x = 0; x < w; ++x)
        {
            const double shift = tilt ? static_cast<double>(x / tilt) - (w / tilt) / 2.0 : 0.0;
            const double bandTop = 24.0 * scale + shift, bandBottom = 40.0 * scale + shift; // plateau [top, bottom)
            std::vector<double> g(static_cast<size_t>(h));
            for (int y = 0; y < h; ++y)
            {
                double a = 0.0;
                if (y >= bandTop && y < bandBottom) a = 1.0;
                else if (y >= bandTop - rampLen && y < bandTop) a = (y - (bandTop - rampLen) + 0.5) / rampLen;
                else if (y >= bandBottom && y < bandBottom + rampLen) a = 1.0 - (y - bandBottom + 0.5) / rampLen;
                g[static_cast<size_t>(y)] = floorLevel + a * (bandLevel - floorLevel);
            }
            const std::vector<uint16_t> column = bayerFromRowProfile(w, h, g);
            for (int y = 0; y < h; ++y)
                full[static_cast<size_t>(y) * w + x] = column[static_cast<size_t>(y) * w + x];
        }
        auto colNyqOfShrink = [&](const std::vector<uint16_t> & reduced) {
            std::vector<uint16_t> rgb(static_cast<size_t>(rw) * rhh * 3, 0u);
            std::vector<float> f(reduced.begin(), reduced.end());
            if (debayerAmaze(rgb.data(), f.data(), rw, rhh, 1, 0) != 1) return -1.0;
            const std::vector<uint8_t> rgb8 = rgb16To8(rgb);
            // Reduced rows 18..45: the band, its gradient edges and two rows of floor each side.
            const std::vector<uint8_t> rows(rgb8.begin() + static_cast<size_t>(18) * rw * 3,
                                            rgb8.begin() + static_cast<size_t>(46) * rw * 3);
            return colNyqEnergy(rows, rw, 28);
        };
        const std::vector<uint16_t> oldReduced = oldDecimator(full, w, h, scale);
        const std::vector<uint16_t> tentReduced = phaseTent(full, w, h, scale);
        ASSERT_FALSE(oldReduced.empty());
        ASSERT_FALSE(tentReduced.empty());
        const double oldNyq = colNyqOfShrink(oldReduced);
        const double tentNyq = colNyqOfShrink(tentReduced);
        std::printf("[gpu-dualiso-preview-scale] T12 x%d %s colNyq through CPU AMaZE: old decimator %.5f, "
                    "phase tent %.5f (bound 0.002)\n", scale, tilt ? "tilted band (report only)" : "band",
                    oldNyq, tentNyq);
        ASSERT_TRUE(oldNyq >= 0.0 && tentNyq >= 0.0);
        if (!tilt) ASSERT_TRUE(tentNyq <= 0.002);
    }
}

// T13 (r4): Gr/Gb balance on a vertical ramp. The site error of an output G is its value
// minus the ramp at its own target centre t_y = S*y + (S-1)/2; the Gr/Gb mismatch of a 2x2
// cell is the Gr error minus the Gb error. The old decimator's mismatch is proportional to
// the slope (3 x slope at x4, 1 x slope at x2); the tent's is <= 1 LSB.
TEST(GpuDualIsoPreviewScale, GrGbBalanceOnVerticalRamp)
{
    for (const int scale : { 2, 4 })
    {
        const int w = 64, h = 256;
        const int ow = w / scale, oh = h / scale;
        double oldMean[2] = { 0.0, 0.0 };
        double tentMax = 0.0;
        const int slopes[2] = { 2, 4 };
        for (int si = 0; si < 2; ++si)
        {
            const int c = slopes[si];
            std::vector<uint16_t> ramp(static_cast<size_t>(w) * h);
            for (int y = 0; y < h; ++y)
                for (int x = 0; x < w; ++x) ramp[static_cast<size_t>(y) * w + x] = static_cast<uint16_t>(1000 + c * y);
            auto mismatch = [&](const std::vector<uint16_t> & out, double * meanAbs, double * maxAbs) {
                double sum = 0.0, mx = 0.0;
                int n = 0;
                for (int y = 2; y + 3 < oh; y += 2) // Gr row y (even), Gb row y+1
                    for (int k = 1; 2 * k + 2 < ow; ++k)
                    {
                        const double grSite = scale * y + (scale - 1) / 2.0;
                        const double gbSite = scale * (y + 1) + (scale - 1) / 2.0;
                        const double gr = out[static_cast<size_t>(y) * ow + 2 * k + 1] - (1000 + c * grSite);
                        const double gb = out[static_cast<size_t>(y + 1) * ow + 2 * k] - (1000 + c * gbSite);
                        const double m = std::fabs(gr - gb);
                        sum += m;
                        mx = std::max(mx, m);
                        ++n;
                    }
                if (meanAbs) *meanAbs = n ? sum / n : 0.0;
                if (maxAbs) *maxAbs = mx;
            };
            double tentMaxHere = 0.0;
            mismatch(oldDecimator(ramp, w, h, scale), &oldMean[si], nullptr);
            mismatch(phaseTent(ramp, w, h, scale), nullptr, &tentMaxHere);
            tentMax = std::max(tentMax, tentMaxHere);
            std::printf("[gpu-dualiso-preview-scale] T13 x%d slope %d: old Gr/Gb mismatch %.3f LSB, tent max %.3f LSB\n",
                        scale, c, oldMean[si], tentMaxHere);
        }
        ASSERT_TRUE(oldMean[0] > 1.0);
        ASSERT_TRUE(std::fabs(oldMean[1] / oldMean[0] - 2.0) <= 0.1);
        ASSERT_TRUE(std::fabs(oldMean[0] - (scale == 4 ? 3.0 : 1.0) * slopes[0]) <= 0.5);
        ASSERT_TRUE(tentMax <= 1.0);
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

// r6 receipt-config parity (opt-in: MLVAPP_C1_RECEIPT_VECTORS_DIR). c1 covered only
// alias ON / chroma OFF; the M16-1243 playback state is alias OFF / chroma 2x2 ON /
// fullres ON / mean23 / dither, and r5d read a per-CFA-site CUDA/CPU gain there. For
// each cell (x2/x4 x chroma {0,1} x alias {0,1}) this writes dll_test vectors from the
// GPU plan's reduced run (phase-tent shrink; in.u16, LUTs, scalars as the backend gets
// them) and the CPU reference on the same bytes (the CPU plan with phaseTentShrink = 1:
// diso_get_full20bit, then the C notch). The exposure match is seeded auto -1 / ev 1 /
// delta -1 before the prime. A cell is the large fixture's frame 2, or, with
// MLVAPP_C1_RECEIPT_CLIP naming a clip.txt ("<clip> <frame>", the clip resolved by ID
// outside the test), the owner clip without a receipt. When a cell holds the CUDA DLL's
// output (cuda_nonotch.u16 from dll_test --dump, cuda_notch.u16 with
// --reduced-iso-notch), the parity is asserted per CFA site: median CUDA/CPU in
// [0.995, 1.005] and r >= 0.999 over pixels >= 32 LSB above black, whole-frame max abs
// <= 4 LSB and mismatches <= 0.5 %; the c1 config (alias ON, chroma OFF) stays at 0 LSB.
// Without the DLL output the cell is dumped only (hosted CI has no GPU).
namespace {

struct SiteParity
{
    double median[4] = { 0, 0, 0, 0 };
    double r[4] = { 0, 0, 0, 0 };
    size_t samples[4] = { 0, 0, 0, 0 };
    int maxAbs = 0;
    size_t mismatches = 0;
};

SiteParity siteParity(const std::vector<uint16_t> & cuda, const std::vector<uint16_t> & cpu,
                      int w, int h, int black16)
{
    SiteParity p;
    std::vector<double> gains[4];
    double sx[4] = { 0 }, sy[4] = { 0 }, sxx[4] = { 0 }, syy[4] = { 0 }, sxy[4] = { 0 };
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const size_t i = static_cast<size_t>(y) * w + x;
            const int d = std::abs(static_cast<int>(cuda[i]) - static_cast<int>(cpu[i]));
            p.mismatches += d != 0;
            p.maxAbs = std::max(p.maxAbs, d);
            const double ref = static_cast<double>(cpu[i]) - black16;
            if (ref < 32.0) continue;
            const double got = static_cast<double>(cuda[i]) - black16;
            const int site = (y & 1) * 2 + (x & 1);
            gains[site].push_back(got / ref);
            sx[site] += ref; sy[site] += got;
            sxx[site] += ref * ref; syy[site] += got * got; sxy[site] += ref * got;
        }
    for (int s = 0; s < 4; ++s)
    {
        const size_t n = gains[s].size();
        p.samples[s] = n;
        if (n == 0) continue;
        std::nth_element(gains[s].begin(), gains[s].begin() + n / 2, gains[s].end());
        p.median[s] = gains[s][n / 2];
        const double cov = sxy[s] - sx[s] * sy[s] / n;
        const double vx = sxx[s] - sx[s] * sx[s] / n;
        const double vy = syy[s] - sy[s] * sy[s] / n;
        p.r[s] = (vx > 0 && vy > 0) ? cov / std::sqrt(vx * vy) : (vx == vy ? 1.0 : 0.0);
    }
    return p;
}

bool readU16(const std::string & path, std::vector<uint16_t> & v, size_t n)
{
    FILE * f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    v.assign(n, 0u);
    const bool ok = std::fread(v.data(), sizeof(uint16_t), n, f) == n;
    std::fclose(f);
    return ok;
}

bool writeU16(const std::string & path, const std::vector<uint16_t> & v)
{
    FILE * f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    const bool ok = std::fwrite(v.data(), sizeof(uint16_t), v.size(), f) == v.size();
    std::fclose(f);
    return ok;
}

} // namespace

TEST(GpuDualIsoPreviewScale, C1ReceiptConfigReconParity)
{
    const char * root = std::getenv("MLVAPP_C1_RECEIPT_VECTORS_DIR");
    if (!root || !*root)
    {
        std::printf("[gpu-dualiso-preview-scale] receipt-config parity skipped (MLVAPP_C1_RECEIPT_VECTORS_DIR unset)\n");
        return;
    }
    struct Source { std::string name; std::string clip; unsigned long long frame; };
    std::vector<Source> sources = { { "large", std::string(), 2ull } };
    if (const char * clipTxt = std::getenv("MLVAPP_C1_RECEIPT_CLIP"))
    {
        char path[1024] = { 0 };
        unsigned long long frame = 0;
        FILE * f = *clipTxt ? std::fopen(clipTxt, "r") : nullptr;
        if (f)
        {
            if (std::fscanf(f, "%1023s %llu", path, &frame) == 2) sources.push_back({ "m16", path, frame });
            std::fclose(f);
        }
        ASSERT_EQ(static_cast<size_t>(2), sources.size());
    }
    size_t failedCells = 0;
    for (const Source & src : sources)
        for (const int scale : { 4, 2 })
            for (const int chroma : { CS_2x2, CS_OFF })
                for (const int alias : { FR_OFF, FR_ON })
                {
                    const std::string cell = src.name + "-x" + std::to_string(scale) + "-c"
                                           + std::to_string(chroma) + "-a" + std::to_string(alias);
                    const std::string dir = std::string(root) + "/" + cell;
                    QDir().mkpath(QString::fromStdString(dir));
                    GpuReconEnv env(true);
                    MlvPipelineFixture fixture;
                    QString error;
                    if (src.clip.empty())
                    {
                        ASSERT_TRUE(fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error));
                        ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"), &error));
                        ASSERT_TRUE(fixture.applyReceipt(&error));
                        fixture.video()->llrawproc->focus_pixels = 0;
                        fixture.video()->llrawproc->bad_pixels = 0;
                        fixture.video()->llrawproc->vertical_stripes = 0;
                    }
                    else
                    {
                        // The owner shape has no receipt: HQ dual-ISO on, everything else default.
                        ASSERT_TRUE(fixture.openClipFile(QString::fromLocal8Bit(src.clip.c_str()), &error));
                        llrpSetFixRawMode(fixture.video(), 1);
                        llrpSetDualIsoMode(fixture.video(), 1);
                        // r7: the app queues the one-shot stripe solve on load (MainWindow's
                        // llrpComputeStripesOn calls); the frame-0 prime below runs it.
                        llrpComputeStripesOn(fixture.video());
                    }
                    mlvObject_t * video = fixture.video();
                    llrpSetDualIsoPlaybackForceMean23(video, 1);
                    llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
                    llrpSetDualIsoAliasMapMode(video, alias);
                    llrpSetDualIsoFullResBlendingMode(video, FR_ON);
                    llrpSetChromaSmoothMode(video, chroma);
                    video->llrawproc->diso_auto_correction = -1;
                    video->llrawproc->diso_ev_correction = 1.0;
                    video->llrawproc->diso_black_delta = -1;
                    ASSERT_FALSE(fixture.renderFrame8(0).empty());

                    // CUDA arm: what the backend receives on the GPU plan's reduced run.
                    {
                        const FakeGpuBackendScope fake;
                        GPU_DUALISO_TEST_SETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR", dir.c_str());
                        const bool ran = gpuReducedFrame(fixture, src.frame, scale, nullptr);
                        GPU_DUALISO_TEST_UNSETENV("MLVAPP_FAKE_GPU_RECON_DUMP_DIR");
                        ASSERT_TRUE(ran);
                    }

                    // CPU arm on the same bytes: the CPU plan with the GPU plan's shrink.
                    GPU_DUALISO_TEST_UNSETENV("MLVAPP_GPU_PLAYBACK_RECON");
                    mlvDualIsoPreviewScaleRecon_t plan;
                    ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(video, scale, &plan));
                    plan.phaseTentShrink = 1;
                    const int w = plan.reducedWidth, h = plan.reducedHeight;
                    const size_t n = static_cast<size_t>(w) * h;
                    std::vector<uint16_t> cpuIn(n, 0u), gpuIn;
                    {
                        std::vector<uint16_t> raw = decodeRaw(fixture, src.frame);
                        ASSERT_FALSE(raw.empty());
                        WorkerState worker;
                        ASSERT_TRUE(mlvDualIsoPreviewScaleReconShrink(video, &plan, raw.data(), cpuIn.data(),
                                                                      &worker.state, 1, nullptr, nullptr) > 0);
                    }
                    ASSERT_TRUE(readU16(dir + "/in.u16", gpuIn, n));
                    ASSERT_TRUE(writeU16(dir + "/cpu_in.u16", cpuIn));
                    // Both routes then share the with-dims prefix (llrawproc.c:5277-5437, incl.
                    // the restricted-range rescale) before the recon, so in.u16 is that prefix
                    // applied to this shrink; recorded, not asserted byte-equal.
                    size_t inMismatches = 0;
                    for (size_t i = 0; i < n; ++i) inMismatches += gpuIn[i] != cpuIn[i];
                    std::printf("[gpu-dualiso-preview-scale] receipt parity %s in.u16 vs CPU-plan tent shrink (pre-prefix): mismatches %zu / %zu\n",
                                cell.c_str(), inMismatches, n);
                    std::vector<uint16_t> raw = decodeRaw(fixture, src.frame);
                    ASSERT_FALSE(raw.empty());
                    std::vector<uint16_t> recon(n, 0u);
                    {
                        WorkerState worker;
                        ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(video, &plan, raw.data(), recon.data(),
                                                                    &worker.state, 1, nullptr, nullptr));
                    }
                    GPU_DUALISO_TEST_SETENV("MLVAPP_GPU_PLAYBACK_RECON", "1");
                    std::vector<uint16_t> notched(n, 0u);
                    dualiso_reduced_iso_period_notch16(notched.data(), recon.data(), w, h);
                    ASSERT_TRUE(writeU16(dir + "/out.u16", notched));
                    ASSERT_TRUE(writeU16(dir + "/out_without_notch.u16", recon));
                    std::printf("[gpu-dualiso-preview-scale] receipt parity %s %dx%d vectors written\n",
                                cell.c_str(), w, h);

                    // Output black in the 16-bit domain: the 20-bit recon black / 16.
                    int black20 = 0;
                    {
                        FILE * f = std::fopen((dir + "/scalars.txt").c_str(), "r");
                        ASSERT_TRUE(f != nullptr);
                        char line[256];
                        while (std::fgets(line, sizeof(line), f))
                            if (std::sscanf(line, "black_level=%d", &black20) == 1) break;
                        std::fclose(f);
                    }
                    ASSERT_TRUE(black20 > 0);
                    const int black16 = black20 / 16;
                    for (const bool withNotch : { false, true })
                    {
                        std::vector<uint16_t> cuda;
                        if (!readU16(dir + (withNotch ? "/cuda_notch.u16" : "/cuda_nonotch.u16"), cuda, n))
                        {
                            std::printf("[gpu-dualiso-preview-scale] receipt parity %s notch %d: no CUDA output, dumped only\n",
                                        cell.c_str(), withNotch ? 1 : 0);
                            continue;
                        }
                        const SiteParity p = siteParity(cuda, withNotch ? notched : recon, w, h, black16);
                        const char * sites[4] = { "s00", "s01", "s10", "s11" };
                        std::printf("[gpu-dualiso-preview-scale] receipt parity %s notch %d:", cell.c_str(),
                                    withNotch ? 1 : 0);
                        for (int s = 0; s < 4; ++s)
                            std::printf(" %s med %.4f r %.5f n %zu |", sites[s], p.median[s], p.r[s], p.samples[s]);
                        std::printf(" max abs %d mismatches %zu / %zu\n", p.maxAbs, p.mismatches, n);
                        bool green = p.maxAbs <= 4 && p.mismatches * 200 <= n;
                        for (int s = 0; s < 4; ++s)
                            green = green && p.median[s] >= 0.995 && p.median[s] <= 1.005 && p.r[s] >= 0.999;
                        if (chroma == CS_OFF && alias == FR_ON) green = green && p.maxAbs == 0;
                        failedCells += green ? 0 : 1;
                    }
                }
    ASSERT_EQ(static_cast<size_t>(0), failedCells);
}

// r8 (VSTRIPES): #295 (LOOK-ASSIST-DUALISO-VSTRIPES-1) skips the vertical-stripe solve and apply on
// dual-ISO frames. Without it the app's one-shot solve (llrpComputeStripesOn on load) rates RG-row
// columns against greens of the other ISO field, the odd-column coefficients clamp near 2.0, and the
// full-res fix pass doubles every odd column above black+64 before the tent shrink: the venue's x4/x2
// comb input (r7). The frame is #295's S-0110 (rows y%4 in {1,2} bright, {0,3} dark), which drives the
// detector to |log2 coeff| >= 0.9 on odd columns. A (the app shape: stripes 1, solve queued) and C
// (stripes 1, no solve) differ only in the queued solve; their reduced recon must be byte-equal at x4
// and x2 on the GPU plan's tent shrink and on the CPU plan's decimators, with no correction stored.
// The non-dual control (#295's S-flat, DISO_INVALID) still solves and corrects; its output hash is
// printed so it can be compared across builds.
namespace {

int vstripesTexture(int x, int y) { return ((x * 7 + y * 3) % 17) - 8; }

// #295's S-0110 (interleaved) and S-flat frames, built in 14-bit units and handed over at the clip's bit
// depth, as the decoder does.
std::vector<uint16_t> vstripesFrame(MlvPipelineFixture & fixture, bool interleaved)
{
    const mlvObject_t * video = fixture.video();
    const int w = fixture.width();
    const int h = fixture.height();
    const int shift = 14 - video->RAWI.raw_info.bits_per_pixel;
    const int black14 = video->RAWI.raw_info.black_level << shift;
    std::vector<uint16_t> frame(static_cast<size_t>(w) * static_cast<size_t>(h));
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
        {
            const int level = interleaved
                ? ((y % 4 == 1 || y % 4 == 2) ? 4800 : 300) + vstripesTexture(x, y)
                : ((x % 8 == 3) ? 2020 : 2000);
            frame[static_cast<size_t>(y) * w + x] = static_cast<uint16_t>((black14 + level) >> shift);
        }
    if (!interleaved) frame[0] = static_cast<uint16_t>((black14 + 3000) >> shift);
    return frame;
}

void printStripeCorrection(const std::string & label, const stripes_correction & sc)
{
    std::printf("[gpu-dualiso-preview-scale] vstripes %s: needed %d coeffs", label.c_str(), sc.correction_needed);
    for (int j = 0; j < 8; ++j) std::printf(" %d", sc.coeffficients[j]);
    std::printf(" (/65536:");
    for (int j = 0; j < 8; ++j) std::printf(" %.5f", static_cast<double>(sc.coeffficients[j]) / 65536.0);
    std::printf(")\n");
}

struct VStripesTentArm
{
    std::vector<uint16_t> reduced;
    stripes_correction after;
    int fullResFixes = 0;
};

// One reduced run of S-0110 on a fresh HQ tiny object: the solve is one-shot and its correction
// persists, so each arm gets its own object.
VStripesTentArm vstripesTentArm(int scale, bool tentShrink, bool solveQueued)
{
    GpuReconEnv env(false);
    MlvPipelineFixture fixture;
    QString error;
    ASSERT_TRUE(fixture.openTinyDualIso(&error));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error));
    ASSERT_TRUE(fixture.applyReceipt(&error));
    mlvObject_t * video = fixture.video();
    ASSERT_TRUE(llrpGetDualIsoValidity(video) != DISO_INVALID);
    video->llrawproc->focus_pixels = 0;
    video->llrawproc->bad_pixels = 0;
    llrpSetVerticalStripeMode(video, 0);
    ASSERT_FALSE(fixture.renderFrame8(0).empty()); // settles the exposure match and the ISO pattern
    // Both arms leave the prime with no correction and nothing queued (the receipt queues a solve).
    std::memset(&video->llrawproc->stripe_corrections, 0, sizeof(video->llrawproc->stripe_corrections));
    video->llrawproc->compute_stripes = 0;
    llrpSetVerticalStripeMode(video, 1);
    if (solveQueued) llrpComputeStripesOn(video);

    VStripesTentArm arm;
    mlvDualIsoPreviewScaleRecon_t plan;
    ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(video, scale, &plan));
    ASSERT_EQ(scale, plan.scale);
    plan.phaseTentShrink = tentShrink ? 1 : 0;
    arm.fullResFixes = plan.fullResFixes;
    std::vector<uint16_t> raw = vstripesFrame(fixture, true);
    arm.reduced.assign(static_cast<size_t>(plan.reducedWidth) * static_cast<size_t>(plan.reducedHeight), 0u);
    WorkerState worker;
    ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(video, &plan, raw.data(), arm.reduced.data(),
                                                &worker.state, 1, nullptr, nullptr));
    arm.after = video->llrawproc->stripe_corrections;
    return arm;
}

size_t countMismatches(const std::vector<uint16_t> & a, const std::vector<uint16_t> & b)
{
    if (a.size() != b.size()) return std::max(a.size(), b.size());
    size_t n = 0;
    for (size_t i = 0; i < a.size(); ++i) n += a[i] != b[i];
    return n;
}

} // namespace

TEST(GpuDualIsoPreviewScale, VerticalStripesLeaveTheDualIsoTentInputAlone)
{
    // The non-dual control first, so its hash prints on every build.
    {
        MlvPipelineFixture fixture;
        QString error;
        ASSERT_TRUE(fixture.openTinyDualIso(&error));
        mlvObject_t * video = fixture.video();
        llrawprocObject_t * shared = video->llrawproc;
        shared->fix_raw = 1;
        shared->focus_pixels = 0;
        shared->bad_pixels = 0;
        shared->pattern_noise = 0;
        shared->dark_frame = 0;
        shared->dual_iso = 0;
        shared->diso_validity = DISO_INVALID;
        llrpSetVerticalStripeMode(video, 1);
        llrpComputeStripesOn(video);
        std::vector<uint16_t> frame = vstripesFrame(fixture, false);
        WorkerState worker;
        applyLLRawProcObjectWorker(video, frame.data(), frame.size() * sizeof(uint16_t), &worker.state, 0);
        printStripeCorrection("non-dual S-flat (DISO_INVALID, mode 1, solve queued)", shared->stripe_corrections);
        std::printf("[gpu-dualiso-preview-scale] vstripes non-dual control output sha256 %s\n",
                    sha256_bytes(frame.data(), frame.size() * sizeof(uint16_t)).c_str());
        ASSERT_EQ(1, shared->stripe_corrections.correction_needed);
    }

    size_t failures = 0;
    for (const int scale : { 4, 2 })
        for (const bool tent : { true, false })
        {
            const std::string label = std::string("x") + std::to_string(scale) + (tent ? " GPU plan (tent)" : " CPU plan");
            const VStripesTentArm a = vstripesTentArm(scale, tent, true);
            const VStripesTentArm c = vstripesTentArm(scale, tent, false);
            ASSERT_EQ(a.fullResFixes, c.fullResFixes);
            printStripeCorrection(label + " A (solve queued)", a.after);
            const size_t differ = countMismatches(a.reduced, c.reduced);
            std::printf("[gpu-dualiso-preview-scale] vstripes %s: fullResFixes %d, A vs C reduced recon %s (%zu / %zu differ)\n",
                        label.c_str(), a.fullResFixes, differ ? "DIFFERS" : "byte-equal", differ, a.reduced.size());
            failures += (differ != 0 ? 1 : 0) + (a.after.correction_needed != 0 ? 1 : 0);
        }
    ASSERT_EQ(static_cast<size_t>(0), failures);
}

// r8: the M16-1243 cell of the same proof, opt-in (MLVAPP_C1_RECEIPT_CLIP names an existing clip.txt,
// "<clip> <frame>"). The app shape (solve queued on load, run by the frame-0 prime) and no solve must hand
// the tent shrink byte-equal reduced input at x4 and x2, with no correction stored. r7 read odd-column
// coefficients of 131069/65536 without #295. Report only: the x1 full-res fix pass's odd/even column gain
// above black+64 (CAST-5's "G1 about 1 EV").
TEST(GpuDualIsoPreviewScale, M16VerticalStripesSolveLeavesTheReducedInputAlone)
{
    const char * clipTxt = std::getenv("MLVAPP_C1_RECEIPT_CLIP");
    if (!clipTxt || !*clipTxt)
    {
        std::printf("[gpu-dualiso-preview-scale] M16 vstripes cell skipped (MLVAPP_C1_RECEIPT_CLIP unset)\n");
        return;
    }
    char path[1024] = { 0 };
    unsigned long long frame = 0;
    FILE * f = std::fopen(clipTxt, "r");
    ASSERT_TRUE(f != nullptr);
    const int fields = std::fscanf(f, "%1023s %llu", path, &frame);
    std::fclose(f);
    ASSERT_EQ(2, fields);

    GpuReconEnv env(false);
    MlvPipelineFixture solved;
    MlvPipelineFixture unsolved;
    for (const bool solve : { true, false })
    {
        MlvPipelineFixture & fixture = solve ? solved : unsolved;
        QString error;
        ASSERT_TRUE(fixture.openClipFile(QString::fromLocal8Bit(path), &error));
        mlvObject_t * video = fixture.video();
        // The owner shape (as C1ReceiptConfigReconParity): HQ dual-ISO, receipt playback state.
        llrpSetFixRawMode(video, 1);
        llrpSetDualIsoMode(video, 1);
        if (solve) llrpComputeStripesOn(video);
        llrpSetDualIsoPlaybackForceMean23(video, 1);
        llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
        llrpSetDualIsoAliasMapMode(video, FR_OFF);
        llrpSetDualIsoFullResBlendingMode(video, FR_ON);
        llrpSetChromaSmoothMode(video, CS_2x2);
        video->llrawproc->diso_auto_correction = -1;
        video->llrawproc->diso_ev_correction = 1.0;
        video->llrawproc->diso_black_delta = -1;
        ASSERT_FALSE(fixture.renderFrame8(0).empty());
        printStripeCorrection(std::string("M16 f") + std::to_string(frame) + (solve ? " app shape (solve queued)" : " no solve"),
                              video->llrawproc->stripe_corrections);
    }

    size_t failures = solved.video()->llrawproc->stripe_corrections.correction_needed != 0 ? 1 : 0;
    for (const int scale : { 4, 2 })
    {
        std::vector<uint16_t> reduced[2];
        for (const bool solve : { true, false })
        {
            MlvPipelineFixture & fixture = solve ? solved : unsolved;
            mlvDualIsoPreviewScaleRecon_t plan;
            ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
            plan.phaseTentShrink = 1;
            std::vector<uint16_t> raw = decodeRaw(fixture, frame);
            ASSERT_FALSE(raw.empty());
            std::vector<uint16_t> & out = reduced[solve ? 0 : 1];
            out.assign(static_cast<size_t>(plan.reducedWidth) * static_cast<size_t>(plan.reducedHeight), 0u);
            WorkerState worker;
            ASSERT_TRUE(mlvDualIsoPreviewScaleReconShrink(fixture.video(), &plan, raw.data(), out.data(),
                                                          &worker.state, 1, nullptr, nullptr) > 0);
        }
        const size_t differ = countMismatches(reduced[0], reduced[1]);
        std::printf("[gpu-dualiso-preview-scale] M16 f%llu x%d tent shrink input: app shape vs no solve %s (%zu / %zu differ)\n",
                    frame, scale, differ ? "DIFFERS" : "byte-equal", differ, reduced[0].size());
        failures += differ != 0 ? 1 : 0;
    }

    // Report only: x1, the full-res fix pass on this frame, gain per column parity above black+64.
    for (const bool solve : { true, false })
    {
        MlvPipelineFixture & fixture = solve ? solved : unsolved;
        mlvObject_t * video = fixture.video();
        const int shift = 14 - video->RAWI.raw_info.bits_per_pixel;
        const double black14 = static_cast<double>(video->RAWI.raw_info.black_level << shift);
        std::vector<uint16_t> raw = decodeRaw(fixture, frame);
        ASSERT_FALSE(raw.empty());
        const std::vector<uint16_t> before = raw;
        {
            WorkerState worker;
            applyLLRawProcObjectWorker(video, raw.data(), raw.size() * sizeof(uint16_t), &worker.state, 1);
        }
        std::vector<double> gains[2];
        for (size_t i = 0; i < raw.size(); ++i)
        {
            const double pre = static_cast<double>(before[i] << shift) - black14;
            if (pre <= 64.0) continue;
            gains[(i % static_cast<size_t>(fixture.width())) & 1].push_back((static_cast<double>(raw[i]) - black14) / pre);
        }
        double median[2] = { 0.0, 0.0 };
        for (int p = 0; p < 2; ++p)
        {
            if (gains[p].empty()) continue;
            std::nth_element(gains[p].begin(), gains[p].begin() + gains[p].size() / 2, gains[p].end());
            median[p] = gains[p][gains[p].size() / 2];
        }
        std::printf("[gpu-dualiso-preview-scale] M16 f%llu x1 full-res fix pass (%s): median gain even %.4f odd %.4f, odd/even %.4f\n",
                    frame, solve ? "app shape" : "no solve", median[0], median[1],
                    median[0] > 0.0 ? median[1] / median[0] : 0.0);
    }
    ASSERT_EQ(static_cast<size_t>(0), failures);
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
