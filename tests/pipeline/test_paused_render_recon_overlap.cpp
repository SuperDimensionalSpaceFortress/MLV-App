// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 r5: the play-stop overlap, at the library level.
// With playback lookahead 0 the phase-3 recon worker and the render thread never run at
// once during playback (render_starts_with_upstream_in_flight=0 on every venue leg). Play
// stop is the exception: RenderFrameThread::runPhase3 takes the paused request as soon as
// no slot is decode-ready or process-ready (RenderFrameThread.cpp:3139), while the recon
// worker may still be reconstructing the last playback frame. The paused request is refused
// by the reuse policy ("not playing") and runs its own decode + dual-ISO recon on the same
// mlvObject_t (renderDebayered16FromSlot -> getMlvRawFrameDebayered).
//
// The test runs exactly those two call sequences on one object, at once, many times: the
// recon worker's decode + llrawproc on its own worker state (decodeFrameForWorker,
// reconFrameForWorker) and the paused render's production dispatch. Every output must equal
// its single-threaded reference byte for byte, and every process heap must validate after.
// A decode-worker thread (decodeFrameForWorker's getMlvRawFrameUint16) runs beside both.
// This was r4's candidate writer for the 0xC0000374 at play stop. It holds on bf2656e4
// (400 tiny iterations clean), so the candidate is refuted at the library level; the test
// stays as the guard that the stop-time overlap is safe.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../platform/qt/Debayered16ReconReusePolicy.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

namespace {

struct OverlapWorkerState
{
    OverlapWorkerState() { llrpInitWorkerState(&state); }
    ~OverlapWorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

bool openOverlapFixture(MlvPipelineFixture & fixture, bool large)
{
    QString error;
    if (large)
    {
        if (!fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error))
            return false;
        if (!fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"), &error))
            return false;
    }
    else
    {
        if (!fixture.openTinyDualIso(&error)) return false;
        if (!fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error))
            return false;
    }
    if (!fixture.applyReceipt(&error)) return false;
    setMlvCpuCores(fixture.video(), 4);
    return true;
}

size_t overlapPixels(MlvPipelineFixture & fixture)
{
    return static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height());
}

// RenderFrameThread::decodeFrameForWorker + reconFrameForWorker (CPU route, full resolution).
void reconWorkerFrame(mlvObject_t * video, uint64_t frame, llrawprocWorkerState_t * worker,
                      std::vector<uint16_t> & rawImage16, size_t pixels)
{
    if (rawImage16.size() < pixels * 3u) rawImage16.resize(pixels * 3u);
    (void)getMlvRawFrameUint16(video, frame, rawImage16.data());
    applyLLRawProcObjectWorker(video, rawImage16.data(), pixels * sizeof(uint16_t), worker, 0);
}

// RenderFrameThread::drawFrame's OutputDebayered16 branch for a paused frame: the policy
// refuses it ("not playing") and today's getMlvRawFrameDebayered runs. The single-frame
// cache is dropped first, as a frame refresh does, so every call decodes and reconstructs.
Debayered16ReconRefusal pausedRenderFrame(mlvObject_t * video, uint64_t frame, std::vector<uint16_t> & rawImage16,
                                          std::vector<float> & scratch, size_t pixels)
{
    if (rawImage16.size() < pixels * 3u) rawImage16.resize(pixels * 3u);
    resetMlvCachedFrame(video);
    // The paused request itself came through the phase-3 workers (the venue log's refusal
    // is "not playing", the check after the recon and decode checks).
    Debayered16ReconReuseInputs reuse;
    reuse.consumeReconnedRaw = true;
    reuse.reconAcquisitionSucceeded = true;
    reuse.playbackActive = false;
    reuse.reducedReconScale = 1;
    reuse.reconFrameNumber = static_cast<uint32_t>(frame);
    reuse.renderFrameNumber = static_cast<uint32_t>(frame);
    reuse.reconBufferComplete = true;
    return renderDebayered16FromSlot(video, static_cast<uint32_t>(frame), reuse, rawImage16.data(), pixels, scratch,
                                     nullptr, nullptr, nullptr, nullptr);
}

bool processHeapsValid()
{
#ifdef _WIN32
    HANDLE heaps[256];
    const DWORD count = GetProcessHeaps(256, heaps);
    for (DWORD i = 0; i < count && i < 256; ++i)
    {
        if (!HeapValidate(heaps[i], 0, nullptr)) return false;
    }
#endif
    return true;
}

// MLVAPP_PAUSED_RECON_OVERLAP_ITERATIONS raises the count for a longer local stress run.
int overlapIterations(int defaultIterations)
{
    bool ok = false;
    const int value = qEnvironmentVariableIntValue("MLVAPP_PAUSED_RECON_OVERLAP_ITERATIONS", &ok);
    return ok && value > 0 ? value : defaultIterations;
}

struct OverlapRun
{
    int reconMismatches = 0;
    int decodeMismatches = 0;
    int pausedMismatches = 0;
    int pausedNotRefused = 0;
    bool heapsValid = true;
};

// r7: sameFrame puts the recon worker on the paused frame itself (the play-stop shape where the
// last playback frame is the one the paused request redraws), so both reconstruct one frame
// index at once instead of neighbouring ones.
OverlapRun runOverlap(bool large, int iterations, bool sameFrame = false)
{
    OverlapRun run;
    MlvPipelineFixture fixture;
    if (!openOverlapFixture(fixture, large))
    {
        run.reconMismatches = -1;
        return run;
    }
    mlvObject_t * video = fixture.video();
    const size_t pixels = overlapPixels(fixture);
    const uint64_t pausedFrame = 3;
    const std::vector<uint64_t> reconFrames = sameFrame ? std::vector<uint64_t>{ pausedFrame } : std::vector<uint64_t>{ 0, 1, 2 };
    const uint64_t decodeFrames[] = { 4, 5 };

    // Single-threaded references, after one warm-up pass so the first run's one-time loads
    // (pixel maps, the published dual-ISO pattern) are behind both paths.
    OverlapWorkerState reconWorker;
    std::vector<uint16_t> reconBuffer;
    std::vector<uint16_t> pausedBuffer;
    std::vector<float> pausedScratch;
    std::vector<std::vector<uint16_t>> reconRef;
    std::vector<std::vector<uint16_t>> decodeRef;
    for (int pass = 0; pass < 2; ++pass)
    {
        reconRef.clear();
        for (uint64_t frame : reconFrames)
        {
            reconWorkerFrame(video, frame, &reconWorker.state, reconBuffer, pixels);
            reconRef.emplace_back(reconBuffer.begin(), reconBuffer.begin() + pixels);
        }
        decodeRef.clear();
        for (uint64_t frame : decodeFrames)
        {
            std::vector<uint16_t> decoded(pixels);
            (void)getMlvRawFrameUint16(video, frame, decoded.data());
            decodeRef.push_back(decoded);
        }
        if (pausedRenderFrame(video, pausedFrame, pausedBuffer, pausedScratch, pixels)
            != Debayered16ReconRefusal::NotPlaying)
            ++run.pausedNotRefused;
    }
    const std::vector<uint16_t> pausedRef(pausedBuffer.begin(), pausedBuffer.begin() + pixels * 3u);

    // RenderFrameThread::decodeFrameForWorker for the next queued frame.
    std::atomic<bool> reconRendersDone(false);
    std::thread decodeThread;
    std::atomic<int> ready(0);
    std::atomic<bool> go(false);
    decodeThread = std::thread([&]() {
        std::vector<uint16_t> buffer(pixels);
        ready.fetch_add(1);
        while (!go.load()) std::this_thread::yield();
        for (size_t i = 0; !reconRendersDone.load(); ++i)
        {
            const size_t index = i % (sizeof(decodeFrames) / sizeof(decodeFrames[0]));
            (void)getMlvRawFrameUint16(video, decodeFrames[index], buffer.data());
            if (buffer != decodeRef[index]) ++run.decodeMismatches;
        }
    });
    std::thread reconThread([&]() {
        std::vector<uint16_t> buffer;
        ready.fetch_add(1);
        while (!go.load()) std::this_thread::yield();
        for (int i = 0; i < iterations; ++i)
        {
            const size_t index = static_cast<size_t>(i) % reconFrames.size();
            reconWorkerFrame(video, reconFrames[index], &reconWorker.state, buffer, pixels);
            if (std::memcmp(buffer.data(), reconRef[index].data(), pixels * sizeof(uint16_t)) != 0)
                ++run.reconMismatches;
        }
    });
    std::thread renderThread([&]() {
        std::vector<uint16_t> buffer;
        std::vector<float> scratch;
        ready.fetch_add(1);
        while (!go.load()) std::this_thread::yield();
        for (int i = 0; i < iterations; ++i)
        {
            if (pausedRenderFrame(video, pausedFrame, buffer, scratch, pixels) != Debayered16ReconRefusal::NotPlaying)
                ++run.pausedNotRefused;
            if (std::memcmp(buffer.data(), pausedRef.data(), pixels * 3u * sizeof(uint16_t)) != 0)
                ++run.pausedMismatches;
        }
    });
    while (ready.load() < 3) std::this_thread::yield();
    go.store(true);
    reconThread.join();
    renderThread.join();
    reconRendersDone.store(true);
    decodeThread.join();
    run.heapsValid = processHeapsValid();
    std::printf("[paused-render-recon-overlap] large=%d same_frame=%d iterations=%d recon_mismatches=%d decode_mismatches=%d "
                "paused_mismatches=%d paused_not_refused=%d heaps_valid=%d\n",
                large ? 1 : 0, sameFrame ? 1 : 0, iterations, run.reconMismatches, run.decodeMismatches, run.pausedMismatches,
                run.pausedNotRefused, run.heapsValid ? 1 : 0);
    return run;
}

void assertOverlapClean(const OverlapRun & run)
{
    ASSERT_EQ(0, run.pausedNotRefused);
    ASSERT_EQ(0, run.reconMismatches);
    ASSERT_EQ(0, run.decodeMismatches);
    ASSERT_EQ(0, run.pausedMismatches);
    ASSERT_TRUE(run.heapsValid);
}

} // namespace

TEST(PausedRenderReconOverlap, TinyDualIsoPausedRenderBesideReconWorkerIsExactAndHeapIntact)
{
    assertOverlapClean(runOverlap(false, overlapIterations(16)));
}

TEST(PausedRenderReconOverlap, LargeDualIsoPausedRenderBesideReconWorkerIsExactAndHeapIntact)
{
    assertOverlapClean(runOverlap(true, overlapIterations(3)));
}

// r7 (CoS #432, ledger 2026-10-15T07:11:50Z): the same-frame case. The paused render and the
// recon worker reconstruct the SAME frame index at once.
TEST(PausedRenderReconOverlap, TinyDualIsoPausedRenderAndReconOnTheSameFrameAreExactAndHeapIntact)
{
    assertOverlapClean(runOverlap(false, overlapIterations(16), true));
}

TEST(PausedRenderReconOverlap, LargeDualIsoPausedRenderAndReconOnTheSameFrameAreExactAndHeapIntact)
{
    assertOverlapClean(runOverlap(true, overlapIterations(3), true));
}
