// Wiring test (PROCESSED8-MS-TELEMETRY-STUCK-1): pins that the render thread's processed8 /
// processed16 stage timers can never be reported as a live per-frame cost when this frame's
// drawFrame did not run that stage. The timers are thread-locals (g_mlv_last_processed8_* in
// video_mlv.c, g_processing_last_* in raw_processing.c) that only the processed8/16 entries
// rewrite; on the OutputDebayered16 path none of them runs, so every playback_smoke.frame row
// used to carry the frame-0 still render's values (processed8_ms=218.839 on all 427 rows of the
// capA-r2 log). drawFrame now resets the processing timers up front and tags the frame with
// processed8_stage_measured / processed16_stage_measured; the smoke rows print "na" and the
// averages divide by the measured-frame count.
// MainWindow.cpp/RenderFrameThread.cpp need a full GUI build (not linked into console_tests), so
// this test reads the sources as text (mirrors test_playback_smoke_foreground_wiring.cpp).
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

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = nextSignature.isEmpty() ? source.length() : source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

QString drawFrameBody()
{
    // drawFrame is the last function in RenderFrameThread.cpp.
    return functionBody(readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp")),
                        QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"),
                        QString());
}

QString notePresentedFrameBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
                        QStringLiteral("void MainWindow::runPlaybackRoutePrewarm("));
}

QString finishSmokeBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
                        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
}

} // namespace

TEST(Processed8TelemetryLiveness, DrawFrameResetsProcessingTimersBeforeAnyProcessingEntry)
{
    const QString body = drawFrameBody();
    ASSERT_FALSE(body.isEmpty());

    const int resetAt = body.indexOf(QStringLiteral("processingResetLastTimingTelemetry();"));
    ASSERT_TRUE(resetAt >= 0);
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("processingResetLastTimingTelemetry();"), resetAt + 1));

    // First real call (a comment may name the entry without a call parenthesis).
    const QRegularExpressionMatch processedEntry =
        QRegularExpression(QStringLiteral("getMlvProcessedFrame\\w*\\(")).match(body);
    ASSERT_TRUE(processedEntry.hasMatch());
    ASSERT_TRUE(resetAt < processedEntry.capturedStart());

    // The GPU texture no-readback S/H refresh also writes the processing timers.
    const int refreshAt = body.indexOf(QStringLiteral("processingRefreshShadowsHighlights"));
    ASSERT_TRUE(refreshAt >= 0);
    ASSERT_TRUE(resetAt < refreshAt);
}

TEST(Processed8TelemetryLiveness, DrawFrameMarksTheStageRanOnlyInsideTheProcessedBranches)
{
    const QString body = drawFrameBody();
    ASSERT_FALSE(body.isEmpty());

    const int p16BranchAt = body.indexOf(
        QStringLiteral("else if ( outputMode == OutputProcessed16 && !slot.rawImage16.empty() )"));
    const int d16BranchAt = body.indexOf(
        QStringLiteral("else if ( outputMode == OutputDebayered16 && !slot.rawImage16.empty() )"));
    const int p8BranchAt = body.indexOf(QStringLiteral("else if( !slot.rawImage8.empty() )"));
    ASSERT_TRUE(p16BranchAt >= 0);
    ASSERT_TRUE(d16BranchAt > p16BranchAt);
    ASSERT_TRUE(p8BranchAt > d16BranchAt);

    const int p16SetAt = body.indexOf(QStringLiteral("renderThreadProcessed16StageRan = true;"));
    const int p8SetAt = body.indexOf(QStringLiteral("renderThreadProcessed8StageRan = true;"));
    ASSERT_TRUE(p16SetAt > p16BranchAt && p16SetAt < d16BranchAt);
    ASSERT_TRUE(p8SetAt > p8BranchAt);
}

TEST(Processed8TelemetryLiveness, DrawFrameInsertsTheMeasuredFlags)
{
    const QString body = drawFrameBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"processed8_stage_measured\")")));
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"processed16_stage_measured\")")));
}

TEST(Processed8TelemetryLiveness, PresentedFrameReadsTheMeasuredFlags)
{
    const QString body = notePresentedFrameBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("telemetryBoolValue( timing, \"processed8_stage_measured\" )")));
    ASSERT_TRUE(body.contains(QStringLiteral("telemetryBoolValue( timing, \"processed16_stage_measured\" )")));
    ASSERT_TRUE(body.contains(QStringLiteral("++m_playbackSmokeProcessed8MeasuredFrames;")));
    ASSERT_TRUE(body.contains(QStringLiteral("++m_playbackSmokeProcessed16MeasuredFrames;")));
}

TEST(Processed8TelemetryLiveness, FrameRowPrintsNaInsteadOfAStaleProcessed8Number)
{
    const QString body = notePresentedFrameBody();
    ASSERT_FALSE(body.isEmpty());
    const int rowAt = body.indexOf(QStringLiteral("playback_smoke.frame session=%1"));
    const int nextRowAt = body.indexOf(QStringLiteral("playback_smoke.request_state session=%1"), rowAt);
    ASSERT_TRUE(rowAt >= 0);
    ASSERT_TRUE(nextRowAt > rowAt);
    const QString row = body.mid(rowAt, nextRowAt - rowAt);
    ASSERT_FALSE(row.contains(QStringLiteral(".arg( processed8Ms, 0, 'f', 3 )")));
    ASSERT_FALSE(row.contains(QStringLiteral(".arg( processed8CoreMathMs, 0, 'f', 3 )")));
    ASSERT_FALSE(row.contains(QStringLiteral(".arg( processed16CoreMathMs, 0, 'f', 3 )")));
    ASSERT_TRUE(row.contains(QStringLiteral("processed8_measured=")));
    ASSERT_TRUE(row.contains(QStringLiteral("processed16_measured=")));
}

TEST(Processed8TelemetryLiveness, Processed8AveragesDivideByTheMeasuredFrameCount)
{
    const QString body = finishSmokeBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(QRegularExpression(QStringLiteral(
        "m_playbackSmokeProcessed8SumMs\\s*/\\s*static_cast<double>\\(\\s*m_playbackSmokePresentedFrames\\s*\\)"))
        .match(body).hasMatch());
    // avgSmokeMs divides by every presented frame; no processed8 sibling may use it.
    ASSERT_FALSE(body.contains(QStringLiteral("avgSmokeMs( m_playbackSmokeProcessed8")));
    ASSERT_TRUE(body.contains(QStringLiteral("m_playbackSmokeProcessed8MeasuredFrames")));
    ASSERT_TRUE(body.contains(QStringLiteral("m_playbackSmokeProcessed16MeasuredFrames")));
    ASSERT_TRUE(body.contains(QStringLiteral("processed8_measured_frames=%5 processed16_measured_frames=%6")));
}

TEST(Processed8TelemetryLiveness, MeasuredFrameCountersAreDeclaredAndResetPerSession)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("int m_playbackSmokeProcessed8MeasuredFrames = 0;")));
    ASSERT_TRUE(header.contains(QStringLiteral("int m_playbackSmokeProcessed16MeasuredFrames = 0;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int beginAt = source.indexOf(QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"));
    ASSERT_TRUE(beginAt >= 0);
    const int sumResetAt = source.indexOf(QStringLiteral("m_playbackSmokeProcessed8SumMs = 0.0;"), beginAt);
    ASSERT_TRUE(sumResetAt > beginAt);
    const QString near = source.mid(sumResetAt, 300);
    ASSERT_TRUE(near.contains(QStringLiteral("m_playbackSmokeProcessed8MeasuredFrames = 0;")));
    ASSERT_TRUE(near.contains(QStringLiteral("m_playbackSmokeProcessed16MeasuredFrames = 0;")));
}
