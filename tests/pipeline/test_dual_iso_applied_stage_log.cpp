/*!
 * \file test_dual_iso_applied_stage_log.cpp
 * \brief EXPORT-RENDER-DUALISO-APPLIED-SIGNAL-1: the rendered export's stage-timing log carries, right after every
 *        llrawproc_dual_iso record, one 0.000 marker record naming whether that llrawproc run applied dual-ISO
 *        (src/mlv/video_mlv.c mlv_note_llrawproc_dual_iso, src/debug/StageTiming.h MLV_STAGE_LLRAWPROC_DUAL_ISO_*).
 *        A child renders one processed16 frame of the tiny dual-ISO fixture; mlv_stage_timing_enabled() caches the
 *        flag for the life of the process, so each setting runs in a re-exec of this binary (the pattern of
 *        test_batch_render_stage_timing.cpp) with its own environment.
 */

#include "../common/minitest.h"

#include "mlv_pipeline_fixture.h"

#include "../../src/debug/StageTiming.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QFile>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <cstring>
#include <iostream>

namespace
{

const char * const kChildModeVar = "MLVAPP_TEST_DUAL_ISO_APPLIED_STAGE_LOG_CHILD";

const char * const kDualIsoStages[] = {
    MLV_STAGE_LLRAWPROC_DUAL_ISO,
    MLV_STAGE_LLRAWPROC_DUAL_ISO_APPLIED,
    MLV_STAGE_LLRAWPROC_DUAL_ISO_NOT_APPLIED,
    MLV_STAGE_LLRAWPROC_DUAL_ISO_NOT_ATTEMPTED,
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
                { QStringLiteral("--gtest_filter=DualIsoAppliedStageLog.ChildRendersUnderTheReExec") });
    ChildRun run;
    if (!child.waitForStarted(30000)) return run;
    run.finished = child.waitForFinished(300000);
    if (!run.finished) child.kill();
    run.output = QString::fromLocal8Bit(child.readAll());
    run.exitCode = child.exitCode();
    return run;
}

// Every llrawproc_dual_iso record is directly followed by exactly one `stage=llrawproc_dual_iso_<outcome> ms=0.000` record of
// its frame, and no marker appears anywhere else. Opening the clip and applying the receipt run llrawproc too (before the
// receipt's dual-ISO settings are live), so the render's own run is the LAST llrawproc_dual_iso record (the one before the
// frame's processed16_total): the line right after it must be exactly `stage=<marker> ms=0.000`.
void assertMarkersFollowEachDualIsoRecord(const QString & path, const QString & marker)
{
    QFile log(path);
    ASSERT_TRUE(log.open(QIODevice::ReadOnly | QIODevice::Text));
    const QStringList lines = QString::fromUtf8(log.readAll()).split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    const QRegularExpression record(QStringLiteral(
        "^\\[mlv-stage-timing\\] frame=(?<f>[0-9]{1,12}) stage=(?<s>[a-z0-9_]{1,64}) ms=(?<ms>[0-9]{1,12}(?:\\.[0-9]{1,9})?)$"));
    const QStringList markers = { QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO_APPLIED),
                                  QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO_NOT_APPLIED),
                                  QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO_NOT_ATTEMPTED) };
    int dualIsoRecords = 0;
    int markerRecords = 0;
    int lastDualIso = -1;
    int processed16 = -1;
    for (int i = 0; i < lines.size(); ++i) {
        const QRegularExpressionMatch hit = record.match(lines[i]);
        if (!hit.hasMatch()) std::cerr << "not a stage record: " << lines[i].toStdString() << "\n";
        ASSERT_TRUE(hit.hasMatch());
        const QString stage = hit.captured(QStringLiteral("s"));
        if (stage == QStringLiteral("processed16_total")) processed16 = i;
        if (markers.contains(stage)) ++markerRecords;
        if (stage != QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO)) continue;
        ++dualIsoRecords;
        lastDualIso = i;
        ASSERT_TRUE(i + 1 < lines.size());
        const QRegularExpressionMatch next = record.match(lines[i + 1]);
        ASSERT_TRUE(next.hasMatch());
        if (!markers.contains(next.captured(QStringLiteral("s")))) {
            std::cerr << "after: " << lines[i].toStdString() << "\nno marker, got: " << lines[i + 1].toStdString() << "\n";
        }
        ASSERT_TRUE(markers.contains(next.captured(QStringLiteral("s"))));
        ASSERT_TRUE(next.captured(QStringLiteral("f")) == hit.captured(QStringLiteral("f")));
        ASSERT_TRUE(next.captured(QStringLiteral("ms")) == QStringLiteral("0.000"));
    }
    ASSERT_TRUE(dualIsoRecords >= 1);
    ASSERT_EQ(dualIsoRecords, markerRecords);
    ASSERT_TRUE(processed16 > lastDualIso);   // the render's run is the last one, inside the rendered frame
    const QRegularExpressionMatch render = record.match(lines[lastDualIso]);
    const QString expected = QStringLiteral("[mlv-stage-timing] frame=%1 stage=%2 ms=0.000")
                                 .arg(render.captured(QStringLiteral("f")), marker);
    if (lines[lastDualIso + 1] != expected) {
        std::cerr << "after: " << lines[lastDualIso].toStdString() << "\nexpected: " << expected.toStdString()
                  << "\ngot: " << lines[lastDualIso + 1].toStdString() << "\n";
    }
    ASSERT_TRUE(lines[lastDualIso + 1] == expected);
}

} // namespace

// Runs only under the re-exec below (the mode variable unset is a no-op pass, so the hosted shards may run it directly).
TEST(DualIsoAppliedStageLog, ChildRendersUnderTheReExec)
{
    const QByteArray mode = qgetenv(kChildModeVar);
    if (mode.isEmpty()) return;

    MlvPipelineFixture fixture;
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
    ASSERT_EQ(1, llrpGetDualIsoMode(fixture.video()));
    if (mode == "not_applied") llrpSetDualIsoInterpolationMethod(fixture.video(), 2);

    mlv_stage_timing_reset_snapshot();
    const std::vector<uint16_t> frame = fixture.renderFrame16(0, 1);
    ASSERT_FALSE(frame.empty());

    // (the snapshot is thread-local and the render may run llrawproc on another thread, so the parent reads the log file; with
    // timing off the file is never opened, and this thread's snapshot carries none of the dual-ISO names)
    if (mode == "off") {
        ASSERT_FALSE(mlv_stage_timing_enabled());
        for (const char * stage : kDualIsoStages) ASSERT_FALSE(snapshotHasStage(stage));
    } else {
        ASSERT_TRUE(mode == "applied" || mode == "not_applied");
        ASSERT_TRUE(mlv_stage_timing_enabled());
    }
}

TEST(DualIsoAppliedStageLog, MarkerFollowsEachDualIsoRecordWhenTimingIsOnAndNothingWhenOff)
{
    if (!qgetenv(kChildModeVar).isEmpty()) return;   // a child never re-executes itself

    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString childPass = QStringLiteral("[PASS] DualIsoAppliedStageLog.ChildRendersUnderTheReExec");

    const QString offFile = dir.filePath(QStringLiteral("stage-timing-off.log"));
    const ChildRun off = runChild(QStringLiteral("off"), offFile, false);
    if (off.exitCode != 0) std::cerr << off.output.toStdString() << "\n";
    ASSERT_TRUE(off.finished);
    ASSERT_EQ(0, off.exitCode);
    ASSERT_TRUE(off.output.contains(childPass));
    ASSERT_FALSE(QFile::exists(offFile));   // timing off: the stage-timing file is never opened

    const QString appliedFile = dir.filePath(QStringLiteral("stage-timing-applied.log"));
    const ChildRun applied = runChild(QStringLiteral("applied"), appliedFile, true);
    if (applied.exitCode != 0) std::cerr << applied.output.toStdString() << "\n";
    ASSERT_TRUE(applied.finished);
    ASSERT_EQ(0, applied.exitCode);
    ASSERT_TRUE(applied.output.contains(childPass));
    assertMarkersFollowEachDualIsoRecord(appliedFile, QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO_APPLIED));

    const QString notAppliedFile = dir.filePath(QStringLiteral("stage-timing-not-applied.log"));
    const ChildRun notApplied = runChild(QStringLiteral("not_applied"), notAppliedFile, true);
    if (notApplied.exitCode != 0) std::cerr << notApplied.output.toStdString() << "\n";
    ASSERT_TRUE(notApplied.finished);
    ASSERT_EQ(0, notApplied.exitCode);
    ASSERT_TRUE(notApplied.output.contains(childPass));
    assertMarkersFollowEachDualIsoRecord(notAppliedFile, QStringLiteral(MLV_STAGE_LLRAWPROC_DUAL_ISO_NOT_APPLIED));
}
