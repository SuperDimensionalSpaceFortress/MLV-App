/*!
 * \file test_batch_render_stage_timing.cpp
 * \brief EXPORT-RENDER-PIPE-TIMING-1: the rendered batch export's stage notes (src/batch/BatchRenderStageTiming.h, the
 *        helper BatchRunner.cpp times export_scale, export_pipe_write, export_pipe_drain and export_verify through).
 *        With MLVAPP_STAGE_TIMING unset the helper reads no clock, writes no thread-local snapshot record and opens no
 *        stage-timing file; with MLVAPP_STAGE_TIMING=1 it writes the exact `[mlv-stage-timing] frame=N stage=NAME ms=X`
 *        line the dual-venue judge parses. mlv_stage_timing_enabled() caches the flag for the life of the process, so
 *        each setting runs in a re-exec of this binary (filtered to the child test) with its own environment.
 */

#include "../common/minitest.h"

#include "../../src/batch/BatchRenderStageTiming.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QFile>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <cstring>
#include <iostream>

namespace
{

const char * const kChildModeVar = "MLVAPP_TEST_BATCH_RENDER_STAGE_TIMING_CHILD";
const uint64_t kChildFrame = 41;

const char * const kExportStages[] = {
    MLV_STAGE_EXPORT_SCALE,
    MLV_STAGE_EXPORT_PIPE_WRITE,
    MLV_STAGE_EXPORT_PIPE_DRAIN,
    MLV_STAGE_EXPORT_VERIFY,
};

bool snapshotHasStage(const char * stage)
{
    const mlv_stage_timing_snapshot_t * snapshot = mlv_stage_timing_get_snapshot();
    for (int i = 0; i < MLV_STAGE_TIMING_SNAPSHOT_CAPACITY; ++i) {
        if (snapshot->entries[i].used && std::strcmp(snapshot->entries[i].stage, stage) == 0) return true;
    }
    return false;
}

struct ChildRun
{
    int exitCode = -1;
    bool finished = false;
    QString output;
};

ChildRun runChild(const QString & mode, const QString & timingFile, bool timingOn)
{
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.remove(QStringLiteral("MLVAPP_STAGE_TIMING"));
    env.remove(QStringLiteral("MLVAPP_STAGE_TIMING_FILE"));
    env.insert(QString::fromLatin1(kChildModeVar), mode);
    env.insert(QStringLiteral("MLVAPP_STAGE_TIMING_FILE"), timingFile);
    if (timingOn) env.insert(QStringLiteral("MLVAPP_STAGE_TIMING"), QStringLiteral("1"));

    QProcess child;
    child.setProcessEnvironment(env);
    child.setProcessChannelMode(QProcess::MergedChannels);
    child.start(QCoreApplication::applicationFilePath(),
                { QStringLiteral("--gtest_filter=BatchRenderStageTiming.ChildRunUnderTheReExec") });
    ChildRun run;
    if (!child.waitForStarted(30000)) return run;
    run.finished = child.waitForFinished(120000);
    if (!run.finished) child.kill();
    run.output = QString::fromLocal8Bit(child.readAll());
    run.exitCode = child.exitCode();
    return run;
}

} // namespace

// Runs only under the re-exec below (the mode variable unset is a no-op pass, so the hosted shards may run it directly).
TEST(BatchRenderStageTiming, ChildRunUnderTheReExec)
{
    const QByteArray mode = qgetenv(kChildModeVar);
    if (mode.isEmpty()) return;

    mlv_stage_timing_reset_snapshot();
    const BatchRenderStageTiming stageTiming;
    const double start = stageTiming.start();
    for (const char * stage : kExportStages) stageTiming.note(stage, kChildFrame, start);

    const mlv_stage_timing_snapshot_t * snapshot = mlv_stage_timing_get_snapshot();
    if (mode == "off") {
        ASSERT_FALSE(stageTiming.on);
        ASSERT_TRUE(start == 0.0);   // no clock read
        ASSERT_FALSE(snapshot->has_frame_index);
        for (int i = 0; i < MLV_STAGE_TIMING_SNAPSHOT_CAPACITY; ++i) ASSERT_FALSE(snapshot->entries[i].used);
    } else {
        ASSERT_TRUE(mode == "on");
        ASSERT_TRUE(stageTiming.on);
        ASSERT_TRUE(start > 0.0);
        ASSERT_TRUE(snapshot->has_frame_index);
        ASSERT_EQ(kChildFrame, snapshot->frame_index);
        for (const char * stage : kExportStages) ASSERT_TRUE(snapshotHasStage(stage));
    }
}

TEST(BatchRenderStageTiming, NotesNothingWhenTimingIsOffAndTheExactRecordWhenOn)
{
    if (!qgetenv(kChildModeVar).isEmpty()) return;   // a child never re-executes itself

    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString offFile = dir.filePath(QStringLiteral("stage-timing-off.log"));
    const QString onFile = dir.filePath(QStringLiteral("stage-timing-on.log"));
    const QString childPass = QStringLiteral("[PASS] BatchRenderStageTiming.ChildRunUnderTheReExec");

    const ChildRun off = runChild(QStringLiteral("off"), offFile, false);
    if (off.exitCode != 0) std::cerr << off.output.toStdString() << "\n";
    ASSERT_TRUE(off.finished);
    ASSERT_EQ(0, off.exitCode);
    ASSERT_TRUE(off.output.contains(childPass));
    ASSERT_FALSE(QFile::exists(offFile));   // timing off: the stage-timing file is never opened

    const ChildRun on = runChild(QStringLiteral("on"), onFile, true);
    if (on.exitCode != 0) std::cerr << on.output.toStdString() << "\n";
    ASSERT_TRUE(on.finished);
    ASSERT_EQ(0, on.exitCode);
    ASSERT_TRUE(on.output.contains(childPass));

    QFile log(onFile);
    ASSERT_TRUE(log.open(QIODevice::ReadOnly | QIODevice::Text));
    const QStringList lines = QString::fromUtf8(log.readAll()).split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    // the judge's record grammar (tools/profiling/dual-venue/DualVenueRunner.psm1 $script:ExportStageLogRecord)
    const QRegularExpression record(QStringLiteral(
        "^\\[mlv-stage-timing\\] frame=(?<f>[0-9]{1,12}) stage=(?<s>[a-z0-9_]{1,64}) ms=(?<ms>[0-9]{1,12}(?:\\.[0-9]{1,9})?)$"));
    QSet<QString> seen;
    for (const QString & line : lines) {
        const QRegularExpressionMatch hit = record.match(line);
        if (!hit.hasMatch()) std::cerr << "not a stage record: " << line.toStdString() << "\n";
        ASSERT_TRUE(hit.hasMatch());
        ASSERT_TRUE(hit.captured(QStringLiteral("f")) == QStringLiteral("41"));
        ASSERT_FALSE(seen.contains(hit.captured(QStringLiteral("s"))));
        seen.insert(hit.captured(QStringLiteral("s")));
    }
    ASSERT_EQ(4, lines.size());
    for (const char * stage : kExportStages) ASSERT_TRUE(seen.contains(QString::fromLatin1(stage)));
}
