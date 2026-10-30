// Wiring test: CPU-DEBAYERED16-HONOUR-SCALE-1. An OutputDebayered16 request reaches the
// recon worker's reduced dual-ISO plan only through the GUI thread's admission
// (debayered16HonourScaleRefusal -> PresentationContext::cpuDebayered16HonourScaleAdmitted),
// and the processed8 branch's reduced plan + run block is byte-for-byte what it was before
// this card (b77baaf0). The pipeline tests Debayered16ReconReuse.HonourScale* drive the admitted
// branch; RenderFrameThread.cpp and MainWindow.cpp need the full GUI build (not linked into
// console_tests), so the sources are read as text (mirrors
// test_debayered16_recon_provenance_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QCryptographicHash>
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

// The body of `signature` up to the next function definition's closing brace.
QString functionBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int next = source.indexOf(QStringLiteral("\n}\n"), at);
    ASSERT_TRUE(next > at);
    return source.mid(at, next - at);
}

} // namespace

TEST(Debayered16HonourScaleWiring, Debayered16ReachesTheReducedPlanOnlyThroughTheAdmission)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp")),
                                      QStringLiteral("void RenderFrameThread::reconFrameForWorker("));
    const int gateAt = body.indexOf(QStringLiteral(
        "else if( entry.request.outputMode != OutputProcessed8\n"
        "              && !( entry.request.outputMode == OutputDebayered16\n"
        "                    && reconContext.cpuDebayered16HonourScaleAdmitted ) )"));
    const int notProcessed8At = body.indexOf(QStringLiteral("QStringLiteral(\"process stage is not processed8\")"));
    const int textureAt = body.indexOf(QStringLiteral(
        "else if( wantsGpuPlaybackReconTextureNoReadback )\n"
        "        {\n"
        "            reducedReconFallbackReason =\n"
        "                QStringLiteral(\"GPU playback recon texture route owns this frame\");"));
    const int planAt = body.indexOf(QStringLiteral("mlvDualIsoPreviewScaleReconPlan( m_pMlvObject,"));
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(notProcessed8At > gateAt);
    ASSERT_TRUE(textureAt > notProcessed8At);
    ASSERT_TRUE(planAt > textureAt);
    // One CPU reduced plan in the worker, and the only admission field it reads is the
    // GUI thread's decision.
    ASSERT_EQ(1, body.count(QStringLiteral("mlvDualIsoPreviewScaleReconPlan(")));
    ASSERT_EQ(1, body.count(QStringLiteral("cpuDebayered16HonourScaleAdmitted")));

    // MainWindow sets the admitted flag in exactly one place, from the admission function.
    const QString mainWindow = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(1, mainWindow.count(QStringLiteral("cpuDebayered16HonourScaleAdmitted =")));
    const int decideAt = mainWindow.indexOf(QStringLiteral("debayered16HonourScaleRefusal( honourScale );"));
    const int setAt = mainWindow.indexOf(QStringLiteral(
        "requestContext.cpuDebayered16HonourScaleAdmitted =\n"
        "            honourScaleRefusal == Debayered16HonourScaleRefusal::None;"));
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(setAt > decideAt);
    const int prepFactorAt = mainWindow.indexOf(QStringLiteral(
        "honourScale.prepFactor = playbackPrepDecideReduced( prepInputs ).factor;"));
    ASSERT_TRUE(prepFactorAt >= 0);
    ASSERT_TRUE(prepFactorAt < decideAt);
}

TEST(Debayered16HonourScaleWiring, Processed8ReducedPlanAndRunBlockIsUnchanged)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp")),
                                      QStringLiteral("void RenderFrameThread::reconFrameForWorker("));
    const int start = body.indexOf(QStringLiteral("            mlvDualIsoPreviewScaleRecon_t reducedPlan;"));
    ASSERT_TRUE(start >= 0);
    const int end = body.indexOf(QStringLiteral("        if( slot.reducedReconScale <= 1 )"), start);
    ASSERT_TRUE(end > start);
    const QString block = body.mid(start, end - start);
    // SHA-256 of the same span at b77baaf0 (LF line endings).
    const QByteArray digest =
        QCryptographicHash::hash(block.toUtf8(), QCryptographicHash::Sha256).toHex();
    ASSERT_EQ(std::string("09cf3dcdd381823d0e61a10048881c1ca1beeda17ece3a882fd063fa32d367b7"),
              std::string(digest.constData()));
    ASSERT_EQ(2696, static_cast<int>(block.size()));
}

TEST(Debayered16HonourScaleWiring, RenderConsumesAReducedSlotOnlyForAnAdmittedRequestAtItsScale)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const int drawAt = source.indexOf(QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"));
    ASSERT_TRUE(drawAt >= 0);
    const QString body = source.mid(drawAt);
    const int admittedAt = body.indexOf(QStringLiteral(
        "reuse.honourScaleAdmitted =\n"
        "                m_activePresentationContext.cpuDebayered16HonourScaleAdmitted\n"
        "                && slot.reducedReconScale == playbackScaleFactor\n"
        "                && !slot.reducedReconOnGpu;"));
    const int dispatchAt = body.indexOf(QStringLiteral("renderDebayered16FromSlot("));
    const int dimsAt = body.indexOf(QStringLiteral("&debayered16Dims );"));
    const int resizeAt = body.indexOf(QStringLiteral("renderedImageWidth = debayered16Dims.width;"));
    ASSERT_TRUE(admittedAt >= 0);
    ASSERT_TRUE(dispatchAt > admittedAt);
    ASSERT_TRUE(dimsAt > dispatchAt);
    ASSERT_TRUE(resizeAt > dimsAt);
}

// CPU-DUALISO-PREVIEW-SCALE-VERIFY-1: the render's inline reduced recon decides before it
// reconstructs, resets the slot first, snapshots what the reduced debayer clears, and hands
// the slot to the same dispatch. Pipeline tests drive the shared helpers; this pins the order
// in drawFrame, which they cannot reach.
TEST(Debayered16HonourScaleWiring, InlineReducedReconRunsBeforeTheDispatchAndKeepsItsTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const int drawAt = source.indexOf(QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"));
    ASSERT_TRUE(drawAt >= 0);
    const QString body = source.mid(drawAt);
    const int phase3At = body.indexOf(QStringLiteral(
        "inlineReduced.phase3Disabled = phase3Mode() == Phase3Mode::Disabled;"));
    const int cacheAt = body.indexOf(QStringLiteral(
        "!m_pMlvObject || mlvRawDebayerCacheMayServeFrame( m_pMlvObject, frameNumber ) != 0;"));
    const int wantedAt = body.indexOf(QStringLiteral("debayered16InlineReducedReconWanted( inlineReduced )"));
    const int resetAt = body.indexOf(QStringLiteral("slot.reducedReconScale = 1;"), wantedAt);
    const int runAt = body.indexOf(QStringLiteral("debayered16RunInlineReducedRecon("));
    const int snapshotAt = body.indexOf(QStringLiteral("captureRawUint16DecodeTelemetry( workerRawTelemetry,"));
    const int scaleKeyAt = body.indexOf(QStringLiteral("QStringLiteral(\"cpu_dualiso_recon_scale\")"));
    const int fallbackKeyAt = body.indexOf(QStringLiteral("QStringLiteral(\"cpu_dualiso_recon_fallback_reason\")"));
    const int admittedAt = body.indexOf(QStringLiteral("reuse.honourScaleAdmitted ="));
    const int applyAt = body.indexOf(QStringLiteral("applyDebayered16InlineReducedRecon( inlineReducedRecon,"));
    const int dispatchAt = body.indexOf(QStringLiteral("renderDebayered16FromSlot("));
    const int timingAt = body.indexOf(QStringLiteral(
        "QStringLiteral(\"debayered16_recon_worker_llrawproc_total_ms\"),\n"
        "            inlineReducedRecon.reconstructed\n"
        "                ? inlineReducedRecon.llrawprocMs"));
    ASSERT_TRUE(phase3At >= 0);
    ASSERT_TRUE(cacheAt > phase3At);
    ASSERT_TRUE(wantedAt > cacheAt);
    ASSERT_TRUE(resetAt > wantedAt);
    ASSERT_TRUE(runAt > resetAt);
    ASSERT_TRUE(snapshotAt > runAt);
    ASSERT_TRUE(scaleKeyAt > snapshotAt);
    ASSERT_TRUE(fallbackKeyAt > scaleKeyAt);
    ASSERT_TRUE(admittedAt > fallbackKeyAt);
    ASSERT_TRUE(applyAt > admittedAt);
    ASSERT_TRUE(dispatchAt > applyAt);
    ASSERT_TRUE(timingAt > dispatchAt);
    // Adversary A1: the fix-pass geometry and duration come from what the recon captured.
    const int fixMsAt = body.indexOf(QStringLiteral(
        "const double llrawprocPreDualIsoFixMs = inlineReconConsumed\n"
        "        ? inlineReducedRecon.preDualIsoFixMs"));
    const int fixActiveAt = body.indexOf(QStringLiteral(
        "const bool preDualIsoFixActive = inlineReconConsumed\n"
        "        ? inlineReducedRecon.preDualIsoFixCompleted"));
    ASSERT_TRUE(fixMsAt > timingAt);
    ASSERT_TRUE(fixActiveAt > fixMsAt);
    // r3 (ADV-A1): the decode and llrawproc records survive the reduced debayer
    // (renderDebayered16FromSlot restores them; pipeline-tested), so the export reads them.
    // The two values outside that restore come from the recon, and the debayer-exclusive
    // split subtracts only what ran inside the render thread's debayered-frame total.
    const int rawMsAt = body.indexOf(QStringLiteral(
        "const double renderThreadRawUint16Ms =\n"
        "        inlineReconConsumed ? 0.0 : getMlvLastRawUint16Milliseconds();"));
    const int llrawprocMsAt = body.indexOf(QStringLiteral(
        "const double llrawprocMs =\n"
        "        inlineReconConsumed ? inlineReducedRecon.llrawprocMs : renderThreadLlrawprocMs;"));
    const int exclusiveAt = body.indexOf(QStringLiteral(
        "- renderThreadRawUint16Ms\n"
        "                                                - renderThreadLlrawprocMs );"));
    ASSERT_TRUE(rawMsAt > fixActiveAt);
    ASSERT_TRUE(llrawprocMsAt > rawMsAt);
    ASSERT_TRUE(exclusiveAt > llrawprocMsAt);
    // One call site: the recon worker never takes this route.
    ASSERT_EQ(1, source.count(QStringLiteral("debayered16RunInlineReducedRecon(")));

    // The decode goes through the raw prefetch, the entry today's full path decodes with.
    const QString policy = readRepoFile(QStringLiteral("platform/qt/Debayered16ReconReusePolicy.h"));
    const QString helper = functionBody(policy, QStringLiteral("inline Debayered16InlineReducedRecon debayered16RunInlineReducedRecon("));
    const int planAt = helper.indexOf(QStringLiteral("mlvDualIsoPreviewScaleReconPlan("));
    const int decodeAt = helper.indexOf(QStringLiteral("getMlvRawFrameUint16( video,"));
    ASSERT_TRUE(planAt >= 0);
    ASSERT_TRUE(decodeAt > planAt);
    ASSERT_FALSE(helper.contains(QStringLiteral("getMlvRawFrameUint16Uncached")));

    // r3: only a recon the calling thread made itself keeps its records (a worker slot's
    // render thread decoded nothing, so its reset stays).
    const QString apply = functionBody(policy, QStringLiteral("inline void applyDebayered16InlineReducedRecon("));
    ASSERT_TRUE(apply.contains(QStringLiteral("in.reconOnCallingThread = recon.reconstructed;")));
    const QString render = functionBody(policy, QStringLiteral("inline Debayered16ReconRefusal renderDebayered16FromSlot("));
    const int saveAt = render.indexOf(QStringLiteral(
        "if( reuse.reconOnCallingThread ) mlvSaveLastDecodeAndLlrawprocTelemetryForCurrentThread();"));
    const int reducedAt = render.indexOf(QStringLiteral("getMlvRawFrameDebayeredFromReducedReconnedRaw16("));
    const int restoreAt = render.indexOf(QStringLiteral(
        "if( reuse.reconOnCallingThread ) mlvRestoreLastDecodeAndLlrawprocTelemetryForCurrentThread();"));
    ASSERT_TRUE(saveAt >= 0);
    ASSERT_TRUE(reducedAt > saveAt);
    ASSERT_TRUE(restoreAt > reducedAt);
    ASSERT_EQ(1, render.count(QStringLiteral("mlvSaveLastDecodeAndLlrawprocTelemetryForCurrentThread")));
}
