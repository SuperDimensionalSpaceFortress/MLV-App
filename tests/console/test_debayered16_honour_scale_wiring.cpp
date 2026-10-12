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
