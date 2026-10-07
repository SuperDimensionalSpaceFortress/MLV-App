// LOOK-ASSIST-DUALISO-VSTRIPES-1: the pre-dual-ISO vertical-stripe correction is skipped on dual-ISO frames.
// stripes.c (imported a1ex code) assumes a plain RGGB frame. It rates the columns of an RG row against a green of
// the next row; on an interleaved dual-ISO frame that row can belong to the other ISO field, so the odd-column
// coefficients measure the field gap (clamped near 2^+-1) and the apply pass scales Gr and B by about 1 EV.
//  1. the mechanism, on stripes.c directly (S-0110: rows y%4 in {1,2} bright, {0,3} dark);
//  2. stripes.c still corrects a real 1 % column stripe on a non-dual frame (S-flat);
//  3. applyLLRawProcObjectPreDualIsoFixes leaves a dual-ISO frame alone in every mode (byte-identical to mode 0);
//  4. a non-dual frame (DISO_INVALID) still gets exactly the stripes.c correction (and S-0110 triggers it, so 3
//     cannot pass on an inert fix);
//  5. on the real HQ fixture the stripe setting changes nothing on the full-res render, the scale-2 subset and the
//     DNG frame payload (may be inert on this fixture; 3 and 4 carry the kill power).
extern "C" {
#include "../../src/mlv/llrawproc/stripes.h"
}
#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/processing/raw_processing.h"

#include <QtGlobal>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern "C" int llrawprocChecked14BitFrameSizeForTesting(int width,
                                                         int height,
                                                         uint32_t * frame_size);

namespace {

const int kFixpOne = 65536;

struct ClipGeometry
{
    int width = 0;
    int height = 0;
    int bpp = 0;
    int black = 0;      // clip units
    int white = 0;      // clip units
    int shift = 0;      // 14 - bpp
    int black14 = 0;    // what the worker uses after make_14bit
    int white14 = 0;
    int32_t frameSize14 = 0;
};

ClipGeometry tinyGeometry()
{
    MlvPipelineFixture fixture;
    QString error;
    ASSERT_TRUE(fixture.openTinyDualIso(&error));
    const mlvObject_t * video = fixture.video();
    ClipGeometry g;
    g.width = video->RAWI.xRes;
    g.height = video->RAWI.yRes;
    g.bpp = video->RAWI.raw_info.bits_per_pixel;
    g.black = video->RAWI.raw_info.black_level;
    g.white = video->RAWI.raw_info.white_level;
    std::printf("[vstripes] tiny fixture %dx%d (raw_info %dx%d) bpp=%d black=%d white=%d frame_size=%u\n", g.width,
                g.height, video->RAWI.raw_info.width, video->RAWI.raw_info.height, g.bpp, g.black, g.white,
                static_cast<unsigned>(video->RAWI.raw_info.frame_size));
    ASSERT_TRUE(g.bpp >= 10 && g.bpp <= 14);
    ASSERT_TRUE(g.width % 8 == 0 && g.height % 2 == 0);  // stripes.c walks 8-pixel blocks and row pairs
    g.shift = 14 - g.bpp;
    g.black14 = g.black << g.shift;
    g.white14 = g.white << g.shift;
    if (g.bpp < 14)
    {
        uint32_t frameSize = 0;
        ASSERT_EQ(1, llrawprocChecked14BitFrameSizeForTesting(g.width, g.height, &frameSize));
        g.frameSize14 = static_cast<int32_t>(frameSize);
    }
    else
    {
        g.frameSize14 = static_cast<int32_t>(video->RAWI.raw_info.frame_size);
    }
    return g;
}

int texture(int x, int y) { return ((x * 7 + y * 3) % 17) - 8; }

// S-0110, in 14-bit units: flat RGGB, rows y%4 in {1,2} at black+4800 and {0,3} at black+300, plus a small
// deterministic texture.
std::vector<uint16_t> s0110Frame14(const ClipGeometry & g)
{
    std::vector<uint16_t> frame(static_cast<size_t>(g.width) * static_cast<size_t>(g.height));
    for (int y = 0; y < g.height; ++y)
    {
        const int level = (y % 4 == 1 || y % 4 == 2) ? 4800 : 300;
        for (int x = 0; x < g.width; ++x)
            frame[static_cast<size_t>(y) * g.width + x] = static_cast<uint16_t>(g.black14 + level + texture(x, y));
    }
    return frame;
}

// S-flat, in 14-bit units: black+2000 everywhere, columns x%8==3 at 1.01x the signal. The apply pass treats the
// frame's brightest value as the clip point (stripes.c:375-393) and leaves pixels at it alone, so one highlight pixel
// (column 0, which the detector never reads) keeps that estimate above the striped column.
std::vector<uint16_t> sFlatFrame14(const ClipGeometry & g)
{
    std::vector<uint16_t> frame(static_cast<size_t>(g.width) * static_cast<size_t>(g.height));
    for (int y = 0; y < g.height; ++y)
        for (int x = 0; x < g.width; ++x)
            frame[static_cast<size_t>(y) * g.width + x] =
                static_cast<uint16_t>(g.black14 + ((x % 8 == 3) ? 2020 : 2000));
    frame[0] = static_cast<uint16_t>(g.black14 + 3000);
    return frame;
}

// The same frame at the clip's bit depth (what the decoder hands applyLLRawProcObject).
std::vector<uint16_t> toClipDepth(const ClipGeometry & g, std::vector<uint16_t> frame14)
{
    for (uint16_t & v : frame14) v = static_cast<uint16_t>(v >> g.shift);
    return frame14;
}

stripes_correction detect(const ClipGeometry & g, std::vector<uint16_t> frame14, int mode)
{
    stripes_correction correction;
    std::memset(&correction, 0, sizeof(correction));
    vertical_stripes_scratch_t scratch;
    std::memset(&scratch, 0, sizeof(scratch));
    ASSERT_EQ(1, compute_vertical_stripes_correction_only(&correction, frame14.data(), g.black14, g.white14,
                                                          g.frameSize14, static_cast<uint16_t>(g.width),
                                                          static_cast<uint16_t>(g.height), mode, &scratch));
    free_vertical_stripes_scratch(&scratch);
    std::printf("[vstripes] coefficients:");
    for (int j = 0; j < 8; ++j) std::printf(" %.5f", static_cast<double>(correction.coeffficients[j]) / kFixpOne);
    std::printf(" needed=%d\n", correction.correction_needed);
    return correction;
}

// The stripes.c oracle: what the worker's compute + apply would do to an already-lifted frame.
std::vector<uint16_t> stripesOracle(const ClipGeometry & g, std::vector<uint16_t> lifted14, int mode)
{
    stripes_correction correction;
    std::memset(&correction, 0, sizeof(correction));
    vertical_stripes_scratch_t scratch;
    std::memset(&scratch, 0, sizeof(scratch));
    ASSERT_EQ(1, compute_vertical_stripes_correction_only(&correction, lifted14.data(), g.black14, g.white14,
                                                          g.frameSize14, static_cast<uint16_t>(g.width),
                                                          static_cast<uint16_t>(g.height), mode, &scratch));
    free_vertical_stripes_scratch(&scratch);
    apply_vertical_stripes_correction_only(&correction, lifted14.data(), g.black14, g.white14,
                                           static_cast<uint16_t>(g.width), static_cast<uint16_t>(g.height));
    return lifted14;
}

struct PreDualIsoRun
{
    std::vector<uint16_t> output;
    stripes_correction before;
    stripes_correction after;
};

// One pre-dual-ISO pass on a fresh object (so the worker's stripe scratch starts where the oracle's does), with
// stripes as the only pre-dual-ISO step.
PreDualIsoRun runPreDualIso(const std::vector<uint16_t> & input, int mode, int validity, int dualIso, bool computeOn)
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
    shared->dual_iso = dualIso;
    shared->diso_validity = validity;
    llrpSetVerticalStripeMode(video, mode);
    if (computeOn) llrpComputeStripesOn(video);

    PreDualIsoRun run;
    run.before = shared->stripe_corrections;
    run.output = input;
    applyLLRawProcObjectPreDualIsoFixes(video, run.output.data(), run.output.size() * sizeof(uint16_t));
    run.after = shared->stripe_corrections;
    return run;
}

bool zeroed(const stripes_correction & c)
{
    if (c.correction_needed) return false;
    for (int j = 0; j < 8; ++j)
        if (c.coeffficients[j]) return false;
    return true;
}

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

void openHqTiny(MlvPipelineFixture & fixture, int stripesMode)
{
    QString error;
    ASSERT_TRUE(fixture.openTinyDualIso(&error));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error));
    ASSERT_TRUE(fixture.applyReceipt(&error));
    ASSERT_TRUE(llrpGetDualIsoValidity(fixture.video()) != DISO_INVALID);
    llrpSetVerticalStripeMode(fixture.video(), stripesMode);
    llrpComputeStripesOn(fixture.video());
}

// #271's scale-2 subset, as RenderFrameThread drives it (plan + run on a worker state, then the process stage).
std::vector<uint8_t> scale2SubsetFrame(MlvPipelineFixture & fixture, uint64_t frame, int * fullResFixes)
{
    const int scale = 2;
    mlvDualIsoPreviewScaleRecon_t plan;
    ASSERT_EQ(1, mlvDualIsoPreviewScaleReconPlan(fixture.video(), scale, &plan));
    ASSERT_EQ(scale, plan.scale);
    *fullResFixes = plan.fullResFixes;
    std::vector<uint16_t> raw(static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height()));
    ASSERT_EQ(0, getMlvRawFrameUint16(fixture.video(), frame, raw.data()));
    std::vector<uint16_t> reduced(static_cast<size_t>(plan.reducedWidth) * static_cast<size_t>(plan.reducedHeight));
    WorkerState worker;
    ASSERT_EQ(1, mlvDualIsoPreviewScaleReconRun(fixture.video(), &plan, raw.data(), reduced.data(),
                                                &worker.state, 1, nullptr, nullptr));
    int w = 0, h = 0;
    mlvFrameOutputDimensions(fixture.video(), scale, &w, &h);
    std::vector<uint8_t> out(static_cast<size_t>(w) * static_cast<size_t>(h) * 3u);
    PreviewEnvelope envelope(scale);
    ASSERT_EQ(1, getMlvProcessedFrame8ScaledFromReducedReconnedRaw16(fixture.video(), frame, reduced.data(),
                                                                     plan.reducedWidth, plan.reducedHeight,
                                                                     plan.scale, out.data(), 1, scale));
    return out;
}

std::string dngPayloadHash(int stripesMode, int rawState)
{
    qunsetenv("MLVAPP_EXPORT_STAGE_PROFILER");
    qunsetenv("MLVAPP_GPU_EXPORT");
    qunsetenv("MLVAPP_GPU_EXPORT_DLL");
    qunsetenv("MLVAPP_CDNG_EXPORT_PAYLOAD_HANDOFF");
    qunsetenv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER");
    MlvPipelineFixture fixture;
    openHqTiny(fixture, stripesMode);
    ASSERT_FALSE(fixture.renderFrame16(0, 1).empty());
    int32_t par[4] = { 1, 1, 1, 1 };
    dngObject_t * dng = initDngObject(fixture.video(), rawState, 1.0, par);
    ASSERT_TRUE(dng != nullptr);
    dngFramePayload_t * payload = buildDngFramePayload(fixture.video(), dng, 0, nullptr);
    ASSERT_TRUE(payload != nullptr);
    ASSERT_TRUE(payload->header_size > 0 && payload->image_size > 0);
    const std::string hash = sha256_bytes(payload->header_buf, payload->header_size)
                           + sha256_bytes(payload->image_buf, payload->image_size);
    freeDngFramePayload(payload);
    freeDngObject(dng);
    return hash;
}

} // namespace

TEST(DualIsoVerticalStripes, DetectorMeasuresTheFieldGapOnAnInterleavedFrame)
{
    const ClipGeometry g = tinyGeometry();
    const stripes_correction c = detect(g, s0110Frame14(g), 2);
    for (const int j : { 1, 3, 5, 7 })
        ASSERT_TRUE(std::fabs(std::log2(static_cast<double>(c.coeffficients[j]) / kFixpOne)) >= 0.9);
    for (const int j : { 2, 4, 6 })
        ASSERT_NEAR(1.0, static_cast<double>(c.coeffficients[j]) / kFixpOne, 0.002);
    ASSERT_EQ(1, c.correction_needed);
}

TEST(DualIsoVerticalStripes, NonDualFrameStillCorrectsRealStripes)
{
    const ClipGeometry g = tinyGeometry();
    std::vector<uint16_t> frame = sFlatFrame14(g);
    const stripes_correction c = detect(g, frame, 2);
    ASSERT_NEAR(1.0 / 1.01, static_cast<double>(c.coeffficients[3]) / kFixpOne, 0.002);
    for (const int j : { 1, 2, 4, 5, 6, 7 })
        ASSERT_NEAR(1.0, static_cast<double>(c.coeffficients[j]) / kFixpOne, 0.002);
    ASSERT_EQ(1, c.correction_needed);

    apply_vertical_stripes_correction_only(&c, frame.data(), g.black14, g.white14,
                                           static_cast<uint16_t>(g.width), static_cast<uint16_t>(g.height));
    for (int y = 0; y < g.height; y += 7)
        for (int x = 3; x + 1 < g.width; x += 8)
        {
            const size_t i = static_cast<size_t>(y) * g.width + x;
            ASSERT_TRUE(std::abs(static_cast<int>(frame[i]) - static_cast<int>(frame[i - 1])) <= 1);
            ASSERT_TRUE(std::abs(static_cast<int>(frame[i]) - static_cast<int>(frame[i + 1])) <= 1);
        }
}

TEST(DualIsoVerticalStripes, SkippedOnDualIsoFramesInThePreDualIsoPass)
{
    const ClipGeometry g = tinyGeometry();
    const std::vector<uint16_t> input = toClipDepth(g, s0110Frame14(g));
    const PreDualIsoRun off = runPreDualIso(input, 0, DISO_VALID, 1, false);
    std::vector<uint16_t> lifted = input;
    for (uint16_t & v : lifted) v = static_cast<uint16_t>(v << g.shift);
    ASSERT_TRUE(off.output == lifted);  // mode 0 only lifts to 14 bit: stripes is the only pre-dual-ISO step

    struct Case { const char * name; int validity; int mode; int dualIso; bool computeOn; };
    const Case cases[] = {
        { "a DISO_VALID mode 1 after compute-on", DISO_VALID, 1, 1, true },
        { "b DISO_VALID mode 2", DISO_VALID, 2, 1, false },
        { "c DISO_FORCED mode 2", DISO_FORCED, 2, 1, false },
        { "d DISO_VALID mode 2 raw view (dual_iso 0)", DISO_VALID, 2, 0, false },
    };
    for (const Case & c : cases)
    {
        const PreDualIsoRun run = runPreDualIso(input, c.mode, c.validity, c.dualIso, c.computeOn);
        const bool same = run.output == off.output;
        std::printf("[vstripes] %s: %s\n", c.name, same ? "identical to mode 0" : "DIFFERS from mode 0");
        ASSERT_TRUE(same);
        if (c.dualIso == 1 && c.validity == DISO_VALID)
        {
            ASSERT_TRUE(zeroed(run.before));
            ASSERT_TRUE(zeroed(run.after));
        }
    }
}

TEST(DualIsoVerticalStripes, NonDualIsoPreDualIsoPassIsUnchanged)
{
    const ClipGeometry g = tinyGeometry();

    // S-0110 with DISO_INVALID (every non-dual clip), forced mode.
    const std::vector<uint16_t> interleaved = toClipDepth(g, s0110Frame14(g));
    const PreDualIsoRun interleavedOff = runPreDualIso(interleaved, 0, DISO_INVALID, 0, false);
    const PreDualIsoRun interleavedForced = runPreDualIso(interleaved, 2, DISO_INVALID, 0, false);
    ASSERT_TRUE(interleavedForced.output != interleavedOff.output);  // S-0110 really triggers the detector
    ASSERT_TRUE(interleavedForced.output == stripesOracle(g, interleavedOff.output, 2));

    // S-flat with DISO_INVALID, mode 1 after compute-on.
    const std::vector<uint16_t> flat = toClipDepth(g, sFlatFrame14(g));
    const PreDualIsoRun flatOff = runPreDualIso(flat, 0, DISO_INVALID, 0, false);
    const PreDualIsoRun flatOn = runPreDualIso(flat, 1, DISO_INVALID, 0, true);
    ASSERT_TRUE(flatOn.output != flatOff.output);
    ASSERT_TRUE(flatOn.output == stripesOracle(g, flatOff.output, 1));
    ASSERT_EQ(1, flatOn.after.correction_needed);
}

TEST(DualIsoVerticalStripes, DualIsoPicturesIgnoreTheStripeSettingOnEveryPath)
{
    qunsetenv("MLVAPP_DISABLE_CPU_DUALISO_PREVIEW_SCALE_RECON");
    qunsetenv("MLVAPP_GPU_PLAYBACK_RECON");

    // (a) full-resolution render, frames 0 and 1.
    {
        MlvPipelineFixture off;
        MlvPipelineFixture forced;
        openHqTiny(off, 0);
        openHqTiny(forced, 2);
        for (const uint64_t frame : { 0u, 1u })
        {
            const std::vector<uint16_t> a = off.renderFrame16(frame, 1);
            const std::vector<uint16_t> b = forced.renderFrame16(frame, 1);
            ASSERT_FALSE(a.empty());
            ASSERT_EQ(sha256_bytes(a.data(), a.size() * sizeof(uint16_t)),
                      sha256_bytes(b.data(), b.size() * sizeof(uint16_t)));
        }
    }

    // (b) preview scale 2 through the scale-2 subset; with stripes on, the full-res fix pass runs.
    {
        MlvPipelineFixture off;
        MlvPipelineFixture forced;
        openHqTiny(off, 0);
        openHqTiny(forced, 2);
        ASSERT_FALSE(off.renderFrame8(0).empty());
        ASSERT_FALSE(forced.renderFrame8(0).empty());
        int offFixes = 0;
        int forcedFixes = 0;
        const std::vector<uint8_t> a = scale2SubsetFrame(off, 1, &offFixes);
        const std::vector<uint8_t> b = scale2SubsetFrame(forced, 1, &forcedFixes);
        std::printf("[vstripes] scale-2 plan fullResFixes: stripes 0 -> %d, stripes 2 -> %d\n", offFixes, forcedFixes);
        ASSERT_EQ(1, forcedFixes);
        ASSERT_EQ(sha256_bytes(a.data(), a.size()), sha256_bytes(b.data(), b.size()));
    }

    // (c) the DNG frame payload.
    for (const int rawState : { UNCOMPRESSED_RAW, COMPRESSED_RAW })
        ASSERT_EQ(dngPayloadHash(0, rawState), dngPayloadHash(2, rawState));
}
