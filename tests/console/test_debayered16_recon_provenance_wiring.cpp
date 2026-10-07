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

#include <QFile>
#include <QString>
#include <QTextStream>

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
