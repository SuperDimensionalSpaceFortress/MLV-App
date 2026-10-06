// CPU-DEBAYERED16-REUSE-PHASE3-RECON-1: during CPU playback on the preview-processing
// route (fast processing for playback, subset, with the preview config enabled) the
// render requests OutputDebayered16. The phase-3 recon worker has already decoded the
// frame and run the full llrawproc (dual-ISO recon included) on its own worker state;
// the render used to throw that away and decode + reconstruct again inside
// getMlvRawFrameDebayered. It now debayers the worker's reconstruction
// (getMlvRawFrameDebayeredFromReconnedRaw16) whenever Debayered16ReconReusePolicy
// admits the frame. RenderFrameThread is not linked into the test targets, so
// playbackRenderStep() below performs exactly its two steps for one frame (the recon
// worker, then the render's OutputDebayered16 branch). The tests pin:
//  (a) byte-exact output (memcmp, zero tolerance) against today's render over >= 8
//      consecutive dual-ISO frames, two fresh objects, AMaZE and bilinear;
//  (b) the render runs no second recon (llrawproc run counter and the thread's
//      llrawproc / decode timings), and the mutation: forcing today's path (the kill
//      switch production honours) makes that check fail;
//  (c) the reuse never reads or writes the single-frame cache, and a paused frame and
//      the export path afterwards give the same bytes as on a fresh object;
//  (d) a frame cache that could serve the frame makes the entry decline untouched;
//  (e) the policy admits only full-resolution playback frames the worker reconstructed.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../platform/qt/Debayered16ReconReusePolicy.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#define DEBAYERED16_REUSE_TEST_SETENV(name, value) _putenv_s((name), (value))
#define DEBAYERED16_REUSE_TEST_UNSETENV(name) _putenv_s((name), "")
#else
#define DEBAYERED16_REUSE_TEST_SETENV(name, value) setenv((name), (value), 1)
#define DEBAYERED16_REUSE_TEST_UNSETENV(name) unsetenv((name))
#endif

namespace {

struct KillSwitchGuard
{
    KillSwitchGuard() { DEBAYERED16_REUSE_TEST_UNSETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV); }
    ~KillSwitchGuard() { DEBAYERED16_REUSE_TEST_UNSETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV); }
};

struct WorkerState
{
    WorkerState() { llrpInitWorkerState(&state); }
    ~WorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

enum class Debayer { Receipt, Amaze, Bilinear };

bool openHqFixture(MlvPipelineFixture & fixture, bool large, Debayer debayer, int cpuCores)
{
    QString error;
    const bool opened = large
        ? fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error)
        : fixture.openTinyDualIso(&error);
    if (!opened) return false;
    if (!fixture.loadReceipt(large ? QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml")
                                   : QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"),
                             &error)) return false;
    if (!fixture.applyReceipt(&error)) return false;
    if (debayer == Debayer::Amaze) setMlvAlwaysUseAmaze(fixture.video());
    if (debayer == Debayer::Bilinear) setMlvDontAlwaysUseAmaze(fixture.video());
    setMlvCpuCores(fixture.video(), cpuCores);
    return true;
}

size_t pixels(MlvPipelineFixture & fixture)
{
    return static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height());
}

// The render thread's per-slot state for one frame.
struct Slot
{
    std::vector<uint16_t> rawImage16;  // W*H*3, like FrameSlot::rawImage16
    std::vector<float> scratch;        // RenderFrameThread::m_debayered16ReconScratch
};

struct StepResult
{
    bool reused = false;
    std::string refusal;
    uint64_t renderLlrawprocRuns = 0;     // full llrawproc runs on the render step
    double renderLlrawprocTotalMs = -1.0;
    double renderLlrawprocMs = -1.0;
    double renderRawUint16Ms = -1.0;
};

// One playback frame as RenderFrameThread runs it on this route: the recon worker
// (decode, then the full-res llrawproc on its persistent worker state, flags 0), then
// the render's OutputDebayered16 branch (policy; copy to the scratch; the reconned-raw
// debayer, or today's getMlvRawFrameDebayered on any refusal).
StepResult playbackRenderStep(MlvPipelineFixture & fixture, uint64_t frame, llrawprocWorkerState_t * worker,
                              Slot & slot, bool playing = true)
{
    const size_t n = pixels(fixture);
    slot.rawImage16.resize(n * 3u);
    (void)getMlvRawFrameUint16(fixture.video(), frame, slot.rawImage16.data());
    applyLLRawProcObjectWorker(fixture.video(), slot.rawImage16.data(), n * sizeof(uint16_t), worker, 0);

    Debayered16ReconReuseInputs reuse;
    reuse.consumeReconnedRaw = true;
    reuse.playbackActive = playing;
    reuse.frameCacheMayServe = mlvRawDebayerCacheMayServeFrame(fixture.video(), frame) != 0;
    reuse.reducedReconScale = 1;
    reuse.reconFrameNumber = static_cast<uint32_t>(frame);
    reuse.renderFrameNumber = static_cast<uint32_t>(frame);
    reuse.reconRequestSerial = 7;
    reuse.renderRequestSerial = 7;
    reuse.reconBufferComplete = slot.rawImage16.size() >= n * 3u;

    StepResult result;
    llrpResetDebugRunCount();
    const char * refusal = Debayered16ReconReusePolicy::ineligibleReason(reuse);
    if (!refusal)
    {
        slot.scratch.resize(n);
        std::copy_n(slot.rawImage16.data(), n, reinterpret_cast<uint16_t *>(slot.scratch.data()));
        result.reused = getMlvRawFrameDebayeredFromReconnedRaw16(fixture.video(), frame, slot.scratch.data(),
                                                                  slot.rawImage16.data()) != 0;
        if (!result.reused) refusal = "debayer of the reconstructed raw declined";
    }
    if (!result.reused)
    {
        result.refusal = refusal;
        getMlvRawFrameDebayered(fixture.video(), frame, slot.rawImage16.data());
    }
    result.renderLlrawprocRuns = llrpGetDebugRunCount();
    result.renderLlrawprocTotalMs = llrpGetLastTotalMilliseconds();
    result.renderLlrawprocMs = getMlvLastLlrawprocMilliseconds();
    result.renderRawUint16Ms = getMlvLastRawUint16Milliseconds();
    return result;
}

// The success gate: the render consumed the recon and ran no decode or llrawproc of its own.
bool renderRanNoSecondRecon(const StepResult & r)
{
    return r.reused
        && r.renderLlrawprocRuns == 0
        && r.renderLlrawprocTotalMs == 0.0
        && r.renderLlrawprocMs == 0.0
        && r.renderRawUint16Ms == 0.0;
}

size_t mismatchedWords(const std::vector<uint16_t> & a, const std::vector<uint16_t> & b, size_t words)
{
    size_t mismatched = 0;
    for (size_t i = 0; i < words; ++i)
        if (a[i] != b[i]) ++mismatched;
    return mismatched;
}

void runExactnessSequence(Debayer debayer, int cpuCores, const char * label)
{
    KillSwitchGuard guard;
    MlvPipelineFixture reference;
    MlvPipelineFixture reused;
    ASSERT_TRUE(openHqFixture(reference, true, debayer, cpuCores));
    ASSERT_TRUE(openHqFixture(reused, true, debayer, cpuCores));
    ASSERT_TRUE(llrpHQDualIso(reference.video()) != 0);
    ASSERT_EQ(reference.width(), reused.width());
    ASSERT_EQ(reference.height(), reused.height());
    const uint64_t frames = getMlvFrames(reference.video());
    ASSERT_TRUE(frames >= 8);
    const size_t words = pixels(reference) * 3u;

    WorkerState worker;
    Slot slot;
    for (uint64_t f = 0; f < frames; ++f)
    {
        const std::vector<uint16_t> expected = reference.renderDebayeredFrame16(f);
        const StepResult step = playbackRenderStep(reused, f, &worker.state, slot);
        const size_t mismatched = mismatchedWords(expected, slot.rawImage16, words);
        std::printf("[debayered16-recon-reuse] %s frame %d: reused=%d refusal='%s' mismatched_words=%zu of %zu\n",
                    label, static_cast<int>(f), step.reused ? 1 : 0, step.refusal.c_str(), mismatched, words);
        ASSERT_TRUE(step.reused);
        ASSERT_EQ(static_cast<size_t>(0), mismatched);
        ASSERT_EQ(0, std::memcmp(expected.data(), slot.rawImage16.data(), words * sizeof(uint16_t)));
    }
}

} // namespace

// (a) Byte-exact over every frame of the 16-frame HQ dual-ISO fixture, AMaZE (the
// debayer the playback route runs) on the multi-threaded path the render thread uses.
TEST(Debayered16ReconReuse, ReuseIsByteExactOverDualIsoSequenceAmaze)
{
    runExactnessSequence(Debayer::Amaze, 4, "amaze x4 threads");
}

// (a) The same with the bilinear (u16) debayer, single-threaded.
TEST(Debayered16ReconReuse, ReuseIsByteExactOverDualIsoSequenceBilinear)
{
    runExactnessSequence(Debayer::Bilinear, 1, "bilinear x1 thread");
}

// (b) The render consumes the recon and runs no decode and no llrawproc of its own
// (before this card it ran both: one full llrawproc per frame on the render
// thread). Mutation: forcing today's path makes exactly this check fail.
TEST(Debayered16ReconReuse, RenderRunsNoSecondReconAndForcingOldPathFailsIt)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false, Debayer::Amaze, 1));
    WorkerState worker;
    Slot slot;
    for (const uint64_t frame : { 0u, 1u })
    {
        const StepResult step = playbackRenderStep(fixture, frame, &worker.state, slot);
        std::printf("[debayered16-recon-reuse] reuse frame %d: llrawproc_runs=%llu llrawproc_total_ms=%.3f raw_uint16_ms=%.3f\n",
                    static_cast<int>(frame), static_cast<unsigned long long>(step.renderLlrawprocRuns),
                    step.renderLlrawprocTotalMs, step.renderRawUint16Ms);
        ASSERT_TRUE(renderRanNoSecondRecon(step));
    }

    DEBAYERED16_REUSE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "1");
    for (const uint64_t frame : { 0u, 1u })
    {
        const StepResult step = playbackRenderStep(fixture, frame, &worker.state, slot);
        std::printf("[debayered16-recon-reuse] forced old path frame %d: refusal='%s' llrawproc_runs=%llu llrawproc_total_ms=%.3f\n",
                    static_cast<int>(frame), step.refusal.c_str(),
                    static_cast<unsigned long long>(step.renderLlrawprocRuns), step.renderLlrawprocTotalMs);
        ASSERT_FALSE(step.reused);
        ASSERT_TRUE(step.renderLlrawprocRuns >= 1);
        ASSERT_FALSE(renderRanNoSecondRecon(step));
    }
}

// (c) The reuse leaves the single-frame cache (rgb_raw_current_frame /
// current_cached_frame, read by export, the WB picker and Look Assist's WB solve)
// exactly as the last paused render left it; afterwards a paused render and the export
// path give the same bytes as a fresh object that never played.
TEST(Debayered16ReconReuse, ReuseLeavesFrameCacheAndPausedAndExportUntouched)
{
    KillSwitchGuard guard;
    MlvPipelineFixture played;
    ASSERT_TRUE(openHqFixture(played, false, Debayer::Amaze, 1));
    const size_t words = pixels(played) * 3u;
    const std::vector<uint16_t> pausedFrame0 = played.renderDebayeredFrame16(0);
    ASSERT_EQ(1, played.video()->current_cached_frame_active);
    ASSERT_EQ(static_cast<uint64_t>(0), static_cast<uint64_t>(played.video()->current_cached_frame));
    const std::vector<uint16_t> cachedBefore(played.video()->rgb_raw_current_frame,
                                             played.video()->rgb_raw_current_frame + words);

    WorkerState worker;
    Slot slot;
    for (const uint64_t frame : { 0u, 1u })
    {
        ASSERT_TRUE(playbackRenderStep(played, frame, &worker.state, slot).reused);
    }
    ASSERT_EQ(1, played.video()->current_cached_frame_active);
    ASSERT_EQ(static_cast<uint64_t>(0), static_cast<uint64_t>(played.video()->current_cached_frame));
    ASSERT_EQ(0, std::memcmp(cachedBefore.data(), played.video()->rgb_raw_current_frame, words * sizeof(uint16_t)));

    // Paused / scrubbed frames take today's path (the policy refuses them).
    const StepResult paused = playbackRenderStep(played, 1, &worker.state, slot, /*playing=*/false);
    ASSERT_FALSE(paused.reused);
    ASSERT_TRUE(paused.refusal.find("not playing") != std::string::npos);
    const std::vector<uint16_t> playedPaused1 = played.renderDebayeredFrame16(1);
    const std::vector<uint16_t> playedExport16 = played.renderFrame16(1);
    const std::vector<uint8_t> playedExport8 = played.renderFrame8(1);

    MlvPipelineFixture fresh;
    ASSERT_TRUE(openHqFixture(fresh, false, Debayer::Amaze, 1));
    ASSERT_TRUE(fresh.renderDebayeredFrame16(0) == pausedFrame0);
    const std::vector<uint16_t> freshPaused1 = fresh.renderDebayeredFrame16(1);
    const std::vector<uint16_t> freshExport16 = fresh.renderFrame16(1);
    const std::vector<uint8_t> freshExport8 = fresh.renderFrame8(1);

    ASSERT_TRUE(playedPaused1 == freshPaused1);
    ASSERT_TRUE(playedExport16 == freshExport16);
    ASSERT_TRUE(playedExport8 == freshExport8);
}

// (d) When the AMaZE frame cache could serve the frame, today's path may copy from it,
// so the reconned-raw entry declines and writes nothing.
TEST(Debayered16ReconReuse, FrameCacheThatMayServeMakesEntryDecline)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false, Debayer::Amaze, 1));
    const size_t n = pixels(fixture);
    ASSERT_EQ(0, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));

    setMlvRawCacheLimitMegaBytes(fixture.video(), 512);
    mlvCacheSetStop(fixture.video(), 0);
    ASSERT_TRUE(getMlvRawCacheLimitFrames(fixture.video()) > 0);
    ASSERT_EQ(1, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));

    std::vector<float> scratch(n, 0.0f);
    std::vector<uint16_t> out(n * 3u, 0xABCDu);
    ASSERT_EQ(0, getMlvRawFrameDebayeredFromReconnedRaw16(fixture.video(), 1, scratch.data(), out.data()));
    ASSERT_TRUE(std::all_of(out.begin(), out.end(), [](uint16_t v) { return v == 0xABCDu; }));

    mlvCacheSetStop(fixture.video(), 1);
    disableMlvCaching(fixture.video());
    ASSERT_EQ(0, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));
}

// (e) The eligibility policy: full-resolution playback frames the recon worker
// reconstructed, CPU debayer, no GPU recon texture route, no frame cache, matching
// frame identity. Everything else keeps today's path with a reason.
TEST(Debayered16ReconReuse, PolicyAdmitsOnlyFullResPlaybackRecon)
{
    KillSwitchGuard guard;
    Debayered16ReconReuseInputs ok;
    ok.consumeReconnedRaw = true;
    ok.playbackActive = true;
    ok.reducedReconScale = 1;
    ok.reconFrameNumber = ok.renderFrameNumber = 42;
    ok.reconRequestSerial = ok.renderRequestSerial = 9;
    ok.reconBufferComplete = true;
    ASSERT_TRUE(Debayered16ReconReusePolicy::ineligibleReason(ok) == nullptr);

    const auto refused = [](Debayered16ReconReuseInputs in) {
        return Debayered16ReconReusePolicy::ineligibleReason(in) != nullptr;
    };
    Debayered16ReconReuseInputs in = ok; in.consumeReconnedRaw = false; ASSERT_TRUE(refused(in));
    in = ok; in.playbackActive = false; ASSERT_TRUE(refused(in));
    in = ok; in.useGpuAmazeDebayer = true; ASSERT_TRUE(refused(in));
    in = ok; in.useGpuBilinearDebayer = true; ASSERT_TRUE(refused(in));
    in = ok; in.gpuPlaybackReconTexturePresentRequested = true; ASSERT_TRUE(refused(in));
    in = ok; in.frameCacheMayServe = true; ASSERT_TRUE(refused(in));
    in = ok; in.reducedReconScale = 2; ASSERT_TRUE(refused(in));
    in = ok; in.reducedReconScale = 4; ASSERT_TRUE(refused(in));
    in = ok; in.reconFrameNumber = 41; ASSERT_TRUE(refused(in));
    in = ok; in.reconRequestSerial = 8; ASSERT_TRUE(refused(in));
    in = ok; in.reconBufferComplete = false; ASSERT_TRUE(refused(in));

    DEBAYERED16_REUSE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "1");
    ASSERT_TRUE(refused(ok));
    DEBAYERED16_REUSE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "0");
    ASSERT_FALSE(refused(ok));
}
