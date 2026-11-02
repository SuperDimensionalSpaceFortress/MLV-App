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
#include <QStringList>
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

// The balanced { ... } block that directly follows `anchor` (only whitespace between them);
// empty when the anchor is missing or is not followed by a block.
QString bracedBlockAfter(const QString & source, const QString & anchor)
{
    const int at = source.indexOf(anchor);
    if (at < 0) return QString();
    int open = at + anchor.length();
    while (open < source.length() && source.at(open).isSpace()) ++open;
    if (open >= source.length() || source.at(open) != QLatin1Char('{')) return QString();
    int depth = 0;
    for (int i = open; i < source.length(); ++i)
    {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open, i - open + 1);
    }
    return QString();
}

int countMatches(const QString & text, const QString & pattern)
{
    int count = 0;
    QRegularExpressionMatchIterator it = QRegularExpression(pattern).globalMatch(text);
    while (it.hasNext())
    {
        it.next();
        ++count;
    }
    return count;
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

    // Two-sided: each flag is set as the first statement of its own branch (comments
    // aside), and nowhere else; the only other assignment is the false declaration.
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "else if \\( outputMode == OutputProcessed16 && !slot\\.rawImage16\\.empty\\(\\) \\)\\s*\\{\\s*"
        "(?://[^\\n]*\\s*)*renderThreadProcessed16StageRan = true;")).match(body).hasMatch());
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "else if\\( !slot\\.rawImage8\\.empty\\(\\) \\)\\s*\\{\\s*"
        "(?://[^\\n]*\\s*)*renderThreadProcessed8StageRan = true;")).match(body).hasMatch());
    ASSERT_TRUE(body.contains(QStringLiteral("bool renderThreadProcessed8StageRan = false;")));
    ASSERT_TRUE(body.contains(QStringLiteral("bool renderThreadProcessed16StageRan = false;")));
    ASSERT_EQ(2, countMatches(body, QStringLiteral("renderThreadProcessed8StageRan\\s*(?:\\|=|&=|=(?!=))")));
    ASSERT_EQ(2, countMatches(body, QStringLiteral("renderThreadProcessed16StageRan\\s*(?:\\|=|&=|=(?!=))")));
}

TEST(Processed8TelemetryLiveness, DrawFrameInsertsTheMeasuredFlags)
{
    const QString body = drawFrameBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"processed8_stage_measured\")")));
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"processed16_stage_measured\")")));
}

TEST(Processed8TelemetryLiveness, DrawFrameMeasuredFlagsCarryTheStageRanState)
{
    // A constant (or any value other than the stage-ran flags) would tag every frame,
    // including Debayered16 frames that never ran the stage, as measured.
    const QString body = drawFrameBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "const bool processed8StageMeasured = renderThreadProcessed8StageRan;")).match(body).hasMatch());
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "const bool processed16StageMeasured =\\s*"
        "renderThreadProcessed8StageRan \\|\\| renderThreadProcessed16StageRan;")).match(body).hasMatch());
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "insert\\(\\s*QStringLiteral\\(\"processed8_stage_measured\"\\),\\s*processed8StageMeasured\\s*\\);"))
        .match(body).hasMatch());
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "insert\\(\\s*QStringLiteral\\(\"processed16_stage_measured\"\\),\\s*processed16StageMeasured\\s*\\);"))
        .match(body).hasMatch());
    // Inserted once each, so no later insert can overwrite the flag.
    ASSERT_EQ(1, body.count(QStringLiteral("QStringLiteral(\"processed8_stage_measured\")")));
    ASSERT_EQ(1, body.count(QStringLiteral("QStringLiteral(\"processed16_stage_measured\")")));
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

TEST(Processed8TelemetryLiveness, PresentedFrameAccumulatesOnlyMeasuredFrames)
{
    // Each counter and sum the measured-only averages divide is updated exactly once, inside
    // the block guarded by its measured flag; dropping the guard would add unmeasured 0.0 rows.
    const QString body = notePresentedFrameBody();
    ASSERT_FALSE(body.isEmpty());
    const QString gated8 = bracedBlockAfter(body, QStringLiteral("if( processed8Measured )"));
    const QString gated16 = bracedBlockAfter(body, QStringLiteral("if( processed16Measured )"));
    ASSERT_FALSE(gated8.isEmpty());
    ASSERT_FALSE(gated16.isEmpty());

    const QStringList updates8 = {
        QStringLiteral("++m_playbackSmokeProcessed8MeasuredFrames;"),
        QStringLiteral("m_playbackSmokeProcessed8SumMs += processed8Ms;"),
        QStringLiteral("m_playbackSmokeProcessed8CacheStoreSumMs += processed8CacheStoreMs;"),
        QStringLiteral("m_playbackSmokeProcessed8SetupSumMs += processed8SetupMs;"),
        QStringLiteral("m_playbackSmokeProcessed8CoreMathSumMs += processed8CoreMathMs;"),
        QStringLiteral("m_playbackSmokeProcessed8LocalToneSumMs += processed8LocalToneMs;"),
        QStringLiteral("m_playbackSmokeProcessed8ThreadingOverheadSumMs += processed8ThreadingOverheadMs;"),
    };
    const QStringList updates16 = {
        QStringLiteral("++m_playbackSmokeProcessed16MeasuredFrames;"),
        QStringLiteral("m_playbackSmokeProcessed16SetupSumMs += processed16SetupMs;"),
        QStringLiteral("m_playbackSmokeProcessed16CoreMathSumMs += processed16CoreMathMs;"),
        QStringLiteral("m_playbackSmokeProcessed16LocalToneSumMs += processed16LocalToneMs;"),
        QStringLiteral("m_playbackSmokeProcessed16ThreadingOverheadSumMs += processed16ThreadingOverheadMs;"),
    };
    for (const QString & update : updates8)
    {
        ASSERT_EQ(1, gated8.count(update));
        ASSERT_EQ(1, body.count(update));
    }
    for (const QString & update : updates16)
    {
        ASSERT_EQ(1, gated16.count(update));
        ASSERT_EQ(1, body.count(update));
    }
    // No other write to these accumulators anywhere in the function.
    ASSERT_EQ(7, countMatches(body, QStringLiteral(
        "m_playbackSmokeProcessed8(?:MeasuredFrames|SumMs|CacheStoreSumMs|SetupSumMs|CoreMathSumMs"
        "|LocalToneSumMs|ThreadingOverheadSumMs)\\s*(?:[-+*/]?=(?!=)|\\+\\+|--)"
        "|(?:\\+\\+|--)\\s*m_playbackSmokeProcessed8MeasuredFrames")));
    ASSERT_EQ(5, countMatches(body, QStringLiteral(
        "m_playbackSmokeProcessed16(?:MeasuredFrames|SetupSumMs|CoreMathSumMs|LocalToneSumMs"
        "|ThreadingOverheadSumMs)\\s*(?:[-+*/]?=(?!=)|\\+\\+|--)"
        "|(?:\\+\\+|--)\\s*m_playbackSmokeProcessed16MeasuredFrames")));
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

TEST(Processed8TelemetryLiveness, FrameRowFormatsEachProcessedFieldThroughItsMeasuredFlag)
{
    const QString body = notePresentedFrameBody();
    ASSERT_FALSE(body.isEmpty());
    // The formatter prints "na" for an unmeasured stage; an always-numeric formatter would
    // print the 0.0 the render thread inserts for a stage it did not run.
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "const auto measuredMsText = \\[\\]\\( bool measured, double ms \\) -> QString\\s*\\{\\s*"
        "return measured \\? QString::number\\( ms, 'f', 3 \\) : QStringLiteral\\(\"na\"\\);\\s*\\};"))
        .match(body).hasMatch());
    ASSERT_EQ(1, body.count(QStringLiteral("const auto measuredMsText")));

    const QString row = body.mid(body.indexOf(QStringLiteral("playback_smoke.frame session=%1")));
    const QStringList processed8Args = {
        QStringLiteral("processed8Ms"), QStringLiteral("processed8SetupMs"),
        QStringLiteral("processed8CoreMathMs"), QStringLiteral("processed8LocalToneMs"),
        QStringLiteral("processed8ThreadingOverheadMs"),
    };
    const QStringList processed16Args = {
        QStringLiteral("processed16SetupMs"), QStringLiteral("processed16CoreMathMs"),
        QStringLiteral("processed16LocalToneMs"), QStringLiteral("processed16ThreadingOverheadMs"),
    };
    for (const QString & arg : processed8Args)
    {
        ASSERT_EQ(1, row.count(QStringLiteral(".arg( measuredMsText( processed8Measured, %1 ) )").arg(arg)));
    }
    for (const QString & arg : processed16Args)
    {
        ASSERT_EQ(1, row.count(QStringLiteral(".arg( measuredMsText( processed16Measured, %1 ) )").arg(arg)));
    }
    ASSERT_EQ(9, row.count(QStringLiteral("measuredMsText(")));
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
    // With no measured frame the average prints "na", never 0.000.
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "return measuredFrames > 0\\s*\\?\\s*QString::number\\( sum / static_cast<double>\\( measuredFrames \\), 'f', 3 \\)"
        "\\s*:\\s*QStringLiteral\\(\"na\"\\);")).match(body).hasMatch());
}

TEST(Processed8TelemetryLiveness, EachAverageHelperPassesItsOwnMeasuredFrameCounterAsTheDivisor)
{
    // The guards above only forbid two spellings of a wrong divisor. A helper whose body is
    // re-pointed at m_playbackSmokePresentedFrames would slip past them, so pin each helper's
    // divisor identity: its body passes its own *MeasuredFrames member and nothing else.
    const QString body = finishSmokeBody();
    ASSERT_FALSE(body.isEmpty());

    struct HelperPin
    {
        const char * anchor;
        const char * ownCounter;
    };
    const HelperPin pins[] = {
        { "const auto avgProcessed8MsText = [&]( double sum ) -> QString",
          "m_playbackSmokeProcessed8MeasuredFrames" },
        { "const auto avgProcessed16MsText = [&]( double sum ) -> QString",
          "m_playbackSmokeProcessed16MeasuredFrames" },
    };
    for (const HelperPin & pin : pins)
    {
        const QString anchor = QString::fromLatin1(pin.anchor);
        const QString counter = QString::fromLatin1(pin.ownCounter);
        ASSERT_EQ(1, body.count(anchor));
        const QString helper = bracedBlockAfter(body, anchor);
        ASSERT_FALSE(helper.isEmpty());
        ASSERT_TRUE(QRegularExpression(QStringLiteral(
            "^\\{\\s*return avgMeasuredMsText\\( sum, %1 \\);\\s*\\}$").arg(counter))
            .match(helper).hasMatch());
        ASSERT_FALSE(helper.contains(QStringLiteral("m_playbackSmokePresentedFrames")));
    }
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
