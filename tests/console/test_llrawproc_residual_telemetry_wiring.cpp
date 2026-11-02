// Wiring test: LLRAWPROC-RESIDUAL-SUBTIMERS-1. The llrawproc residual sub-stage fields (llrawproc.h
// llrpResidualSubstageTiming_t, emitted by RenderFrameThread's insertLlrawprocResidualSubstageTelemetry) are written
// twice per frame: by the recon worker after its llrawproc run, and by the render thread in drawFrame. Adversary A1
// (r1, e50b197c): on a CPU route whose frame the recon worker already reconstructed, the render thread runs no
// llrawproc of its own -- every reconned-raw consumer zeroes getMlvLastLlrawprocMilliseconds(), but not all of them
// reset this thread's llrp record -- so an unconditional render-thread insert replaced the worker's fields with an empty
// or stale record. These pins keep the render-thread insert behind "this thread ran llrawproc", keep the worker's insert
// unconditional, and keep every field inside the llrawproc_ prefix the CUDA texture route preserves
// (isGpuPlaybackReconTimingTelemetryKey). RenderFrameThread.cpp needs the full GUI build (not linked into
// console_tests), so this test reads the source as text (mirrors test_pre_dualiso_telemetry_reset_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
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

int countOccurrences(const QString & haystack, const QString & needle)
{
    int count = 0;
    int from = 0;
    while (true) {
        const int at = haystack.indexOf(needle, from);
        if (at < 0) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

QString renderFrameThreadSource()
{
    return readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
}

// drawFrame is the last member function in RenderFrameThread.cpp, so its body runs to the end of the file.
QString drawFrameBody()
{
    const QString source = renderFrameThreadSource();
    const int at = source.indexOf(QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"));
    ASSERT_TRUE(at >= 0);
    return source.mid(at);
}

QString functionBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int open = source.indexOf(QLatin1Char('{'), at);
    ASSERT_TRUE(open >= 0);
    int depth = 0;
    for (int i = open; i < source.size(); ++i) {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open, i - open + 1);
    }
    ASSERT_TRUE(false);
    return QString();
}

const QString kRenderInsert = QStringLiteral("insertLlrawprocResidualSubstageTelemetry( slot.stageTimingTelemetry )");

} // namespace

TEST(LlrawprocResidualTelemetryWiring, RenderThreadInsertOnlyWhenThisThreadRanLlrawproc)
{
    const QString body = drawFrameBody();
    ASSERT_EQ(1, countOccurrences(body, kRenderInsert));
    const int insertAt = body.indexOf(kRenderInsert);
    // The insert is the true arm of a ternary on the render thread's own llrawproc wall (zeroed by every
    // reconned-raw consumer), and the false arm subtracts nothing.
    const QRegularExpression guarded(
        QStringLiteral("const double llrawprocResidualSubstageMs = llrawprocMs > 0\\.0\\s*\\?\\s*"
                       "insertLlrawprocResidualSubstageTelemetry\\( slot\\.stageTimingTelemetry \\)\\s*:\\s*0\\.0;"));
    const QRegularExpressionMatch match = guarded.match(body);
    ASSERT_TRUE(match.hasMatch());
    ASSERT_TRUE(match.capturedStart() < insertAt && insertAt < match.capturedEnd());
    // The guarded sum, not a second call, feeds the render-thread residual.
    const int knownAt = body.indexOf(QStringLiteral("const double llrawprocKnownMs ="));
    const int otherAt = body.indexOf(QStringLiteral("const double llrawprocOtherMs = qMax( 0.0, llrawprocMs - llrawprocKnownMs );"));
    ASSERT_TRUE(match.capturedEnd() <= knownAt && knownAt < otherAt);
    ASSERT_TRUE(body.mid(knownAt, otherAt - knownAt).contains(QStringLiteral("llrawprocResidualSubstageMs")));
    // The worker's value must be read off the slot before the render thread could overwrite it: the
    // llrawprocMs it guards on is the render thread's own wall.
    ASSERT_TRUE(body.indexOf(QStringLiteral("const double llrawprocMs = getMlvLastLlrawprocMilliseconds();")) < match.capturedStart());
}

TEST(LlrawprocResidualTelemetryWiring, ReconWorkerInsertIsUnconditionalAndInItsResidual)
{
    const QString body = functionBody(renderFrameThreadSource(),
                                      QStringLiteral("void insertLlrawprocReconTimingTelemetry( QJsonObject &target )"));
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("+ insertLlrawprocResidualSubstageTelemetry( target );")));
    const int knownAt = body.indexOf(QStringLiteral("const double llrawprocKnownMs ="));
    const int insertAt = body.indexOf(QStringLiteral("insertLlrawprocResidualSubstageTelemetry( target )"));
    const int otherAt = body.indexOf(QStringLiteral("QStringLiteral(\"llrawproc_other_ms\")"));
    ASSERT_TRUE(knownAt >= 0 && knownAt < insertAt && insertAt < otherAt);
}

TEST(LlrawprocResidualTelemetryWiring, EveryResidualFieldCarriesThePreservedPrefix)
{
    const QString body = functionBody(renderFrameThreadSource(),
                                      QStringLiteral("double insertLlrawprocResidualSubstageTelemetry( QJsonObject &target )"));
    const QRegularExpression key(QStringLiteral("QStringLiteral\\(\"([a-z0-9_]+)\"\\)"));
    int keys = 0;
    for (QRegularExpressionMatchIterator it = key.globalMatch(body); it.hasNext();) {
        const QString name = it.next().captured(1);
        ASSERT_TRUE(name.startsWith(QStringLiteral("llrawproc_")));
        ++keys;
    }
    ASSERT_EQ(8, keys);
    // The CUDA texture route keeps the worker's values only through this prefix.
    ASSERT_TRUE(renderFrameThreadSource().contains(
        QStringLiteral("return key.startsWith( QStringLiteral(\"llrawproc_\") )")));
}
