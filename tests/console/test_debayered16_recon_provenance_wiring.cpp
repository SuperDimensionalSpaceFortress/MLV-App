// Wiring test: pins the two RenderFrameThread ends of the debayered-16 recon-provenance handoff.
// CPU-DEBAYERED16-REUSE-PHASE3-RECON-1 r3: the recon worker stamps how a slot's recon was made
// (debayered16ReconDoneStamp: the HQ dual-ISO state, the llrawproc settings, and whether the CUDA
// playback recon ran) in signalReconDoneFromWorker, and drawFrame copies the slot's provenance into
// the policy inputs (applyDebayered16ReconProvenance) before the shared dispatch. The pipeline test
// Debayered16ReconReuse.CudaReconProvenanceIsCarriedToThePolicyAndRefused drives both helpers;
// this test pins that production calls them. RenderFrameThread.cpp needs the full GUI build (not
// linked into console_tests), so the source is read as text (mirrors
// test_pre_dualiso_telemetry_reset_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/Debayered16ReconReusePolicy.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <thread>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString renderFrameThreadSource()
{
    return readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
}

// The body of `signature` up to the next member function definition.
QString memberBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int next = source.indexOf(QStringLiteral("\n}\n"), at);
    ASSERT_TRUE(next > at);
    return source.mid(at, next - at);
}

} // namespace

TEST(Debayered16ReconProvenanceWiring, ReconDoneSignalStampsTheSlotFromTheSharedStamp)
{
    const QString body = memberBody(renderFrameThreadSource(),
                                    QStringLiteral("void RenderFrameThread::signalReconDoneFromWorker( int slotIndex )"));
    const int stampAt = body.indexOf(QStringLiteral("debayered16ReconDoneStamp( m_pMlvObject )"));
    const int lockAt = body.indexOf(QStringLiteral("QMutexLocker locker( &m_mutex );"));
    const int storeAt = body.indexOf(QStringLiteral("m_frameSlots[slotIndex].reconProvenance.done = reconDone;"));
    const int readyAt = body.indexOf(QStringLiteral("m_processReadySlots.push_back( slotIndex );"));
    ASSERT_TRUE(stampAt >= 0);
    ASSERT_TRUE(lockAt >= 0);
    ASSERT_TRUE(storeAt >= 0);
    ASSERT_TRUE(readyAt >= 0);
    // Read on the recon thread before the render-thread mutex (the fingerprint takes
    // llrawproc_mutex), stored before the slot is published as process-ready.
    ASSERT_TRUE(stampAt < lockAt);
    ASSERT_TRUE(storeAt < readyAt);
}

TEST(Debayered16ReconProvenanceWiring, DrawFrameCopiesTheSlotProvenanceIntoThePolicyInputs)
{
    const QString source = renderFrameThreadSource();
    const int drawAt = source.indexOf(QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"));
    ASSERT_TRUE(drawAt >= 0);
    const QString body = source.mid(drawAt);
    const int saveAt = body.indexOf(QStringLiteral("const Debayered16ReconProvenance reconProvenance = slot.reconProvenance;"));
    const int resetAt = body.indexOf(QStringLiteral("slot.resetMetadata();"));
    const int applyAt = body.indexOf(QStringLiteral("applyDebayered16ReconProvenance( reconProvenance, reuse );"));
    const int dispatchAt = body.indexOf(QStringLiteral("renderDebayered16FromSlot("));
    ASSERT_TRUE(saveAt >= 0);
    ASSERT_TRUE(resetAt >= 0);
    ASSERT_TRUE(applyAt >= 0);
    ASSERT_TRUE(dispatchAt >= 0);
    // Saved before resetMetadata clears it, applied before the dispatch reads it.
    ASSERT_TRUE(saveAt < resetAt);
    ASSERT_TRUE(applyAt < dispatchAt);
    // Nothing overwrites a provenance input between the copy and the dispatch.
    const QString between = body.mid(applyAt, dispatchAt - applyAt);
    ASSERT_TRUE(between.indexOf(QStringLiteral("reuse.reconUsedGpuPlaybackRecon")) < 0);
    ASSERT_TRUE(between.indexOf(QStringLiteral("reuse.reconHqDualIso")) < 0);
    ASSERT_TRUE(between.indexOf(QStringLiteral("reuse.reconAcquisitionSucceeded")) < 0);
    ASSERT_TRUE(between.indexOf(QStringLiteral("reuse.reconSettingsAt")) < 0);
}

// PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 r2 (sol r1 B2). A paused warm counts as pipeline work only while
// RenderFrameThread::m_warmRunning holds; warmReconForWorker clears it, and only then does the worker call
// signalReconDoneFromWorker( -1 ). A clip close (MainWindow::freeActiveMlvObjectAfterLifecycleBarrier) waits for
// isIdle() / lock() and then frees the clip. The interleaving below is deterministic (a mutex and a condition variable,
// no sleeps): the worker clears the guard and stops at an injected pause point; the "GUI" waits for idle, closes the
// clip, then resumes the worker, which completes the entry through debayered16ReconDoneStampForEntry.
namespace
{

struct WarmCompletionInterleaving
{
    std::mutex mutex;
    std::condition_variable wake;
    bool warmRunning = true;   // the take set it (takeReconRequestForWorker)
    bool clipClosed = false;
    bool resumeWorker = false;
    int clipReads = 0;
    int clipReadsAfterClose = 0;

    // the clip read the stamp makes (debayered16ReconDoneStamp( m_pMlvObject ) in the app)
    Debayered16ReconDoneStamp readClip()
    {
        std::lock_guard<std::mutex> lock(mutex);
        ++clipReads;
        if (clipClosed) ++clipReadsAfterClose;
        Debayered16ReconDoneStamp stamp;
        stamp.settings = 0x5EEDu;
        return stamp;
    }

    template <typename Complete>
    void run(Complete complete)
    {
        std::thread worker([this, &complete]()
        {
            {
                std::lock_guard<std::mutex> lock(mutex);
                warmRunning = false;   // warmReconForWorker's last critical section
            }
            wake.notify_all();
            {
                // the injected pause point: between the guard release and the completion's read
                std::unique_lock<std::mutex> lock(mutex);
                wake.wait(lock, [this]() { return resumeWorker; });
            }
            complete();   // signalReconDoneFromWorker( -1 )'s stamp
        });
        {
            std::unique_lock<std::mutex> lock(mutex);
            wake.wait(lock, [this]() { return !warmRunning; });   // isIdle() is true now
            clipClosed = true;                                     // freeMlvObject( m_pMlvObject )
            resumeWorker = true;
        }
        wake.notify_all();
        worker.join();
    }
};

} // namespace

TEST(Debayered16ReconProvenanceWiring, AWarmEntryCompletedAfterAClipCloseReadsNothingOfTheClip)
{
    WarmCompletionInterleaving warm;
    Debayered16ReconDoneStamp stamp;
    stamp.settings = 1;
    warm.run([&]() { stamp = debayered16ReconDoneStampForEntry(-1, [&]() { return warm.readClip(); }); });
    printf("[B2] warm completion: clip_reads=%d after_close=%d\n", warm.clipReads, warm.clipReadsAfterClose);
    ASSERT_TRUE(warm.clipClosed);
    ASSERT_EQ(0, warm.clipReads);
    ASSERT_EQ(0, warm.clipReadsAfterClose);
    ASSERT_EQ((uint64_t)0, stamp.settings);   // the default (unused: there is no slot to store it in)
}

// The harness has teeth: the a93ee090 completion (the stamp read unconditionally, slot or not) reads the closed clip.
TEST(Debayered16ReconProvenanceWiring, TheInterleavingCatchesAnUnconditionalStampRead)
{
    WarmCompletionInterleaving warm;
    warm.run([&]() { (void)warm.readClip(); });
    printf("[B2] unconditional stamp (a93ee090 shape): clip_reads=%d after_close=%d\n", warm.clipReads, warm.clipReadsAfterClose);
    ASSERT_EQ(1, warm.clipReadsAfterClose);
}

// A slot entry is still stamped from the clip (its slot keeps the pipeline busy until it is published).
TEST(Debayered16ReconProvenanceWiring, ASlotEntryIsStillStampedFromTheClip)
{
    WarmCompletionInterleaving slot;
    const Debayered16ReconDoneStamp stamp = debayered16ReconDoneStampForEntry(0, [&]() { return slot.readClip(); });
    ASSERT_EQ(1, slot.clipReads);
    ASSERT_EQ((uint64_t)0x5EEDu, stamp.settings);
}
