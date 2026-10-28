// CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 r3 (key3 r1 INV-1/INV-2 = Dot B2/B3, catalogue row CO-01): a clip open
// that a busy playback-prep task refuses must leave the retained clip and every receipt alone, and no caller may
// treat the refusal as a corrupt file. MainWindow cannot be built headless, so each caller is covered twice: its
// decision runs through ClipOpenAbortPolicy.h on a model of the caller's state, and a wiring test pins the real
// caller body in MainWindow.cpp to that decision (each wiring test is red on f5baf5ef).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ClipOpenAbortPolicy.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <cstdio>
#include <vector>

namespace
{

constexpr int kMlvErrNone = 0;
constexpr int kMlvErrCorrupted = 3; // MLV_ERR_CORRUPTED in src/mlv/video_mlv.h

QString mainWindowSource()
{
    const QString path = repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp"));
    QFile file(path);
    if (path.isEmpty() || !file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream in(&file);
    return in.readAll();
}

QString bodyBetween(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

// A session as the clip-switch path sees it: one receipt value per clip, the sliders, the active row and
// the clip whose raw data is loaded.
struct SessionModel
{
    std::vector<int> receipts;
    std::vector<bool> neverLoaded;
    int sliders = 0;
    int active = 0;
    int loaded = 0;
};

// showFileInEditor( row ) with openMlv returning openResult, step by step as in MainWindow.cpp.
void showFileInEditorModel(SessionModel & s, int row, int openResult, int depth = 0)
{
    ASSERT_TRUE(depth < 4); // the fallback recursion is bounded
    if (!s.neverLoaded[s.active]) s.receipts[s.active] = s.sliders; // setReceipt( ACTIVE_RECEIPT )
    const int oldActive = s.active;
    s.active = row; // SET_ACTIVE_CLIP_IDX( row )
    if (openResult)
    {
        if (openResult == kClipOpenAbortedPrepBusyAfterFree) s.loaded = -1;
        const ShowFileOpenFailurePlan plan = showFileOpenFailurePlan(
            openResult, row, oldActive, 1, static_cast<int>(s.receipts.size()), s.neverLoaded[oldActive]);
        if (plan.restoreOldActive) s.active = oldActive;
        if (plan.reopenOld) showFileInEditorModel(s, oldActive, kMlvErrNone, depth + 1);
        return;
    }
    s.loaded = row;
    s.neverLoaded[row] = false;
    s.sliders = s.receipts[row]; // setSliders( GET_RECEIPT( row ) )
}

SessionModel twoLoadedClipsShowingA()
{
    SessionModel s;
    s.receipts = {11, 22}; // A graded 11, B graded 22
    s.neverLoaded = {false, false};
    s.sliders = 11;
    s.active = 0;
    s.loaded = 0;
    return s;
}

} // namespace

// (a) clip switch to a previously loaded clip while the prep drain times out.
TEST(ClipOpenAbortCallers, ClipSwitchRefusedByPrepKeepsBothReceiptsAndTheLoadedClip)
{
    SessionModel s = twoLoadedClipsShowingA();
    showFileInEditorModel(s, 1, kClipOpenAbortedPrepBusy);
    ASSERT_EQ(s.receipts[1], 22); // B's grade is not overwritten with A's sliders
    ASSERT_EQ(s.receipts[0], 11);
    ASSERT_EQ(s.active, 0);       // A, which is still loaded, is the active row again
    ASSERT_EQ(s.loaded, 0);
    ASSERT_EQ(s.sliders, 11);
    // the next switch away from A saves A's sliders into A, not into B
    showFileInEditorModel(s, 1, kMlvErrNone);
    ASSERT_EQ(s.receipts[0], 11);
    ASSERT_EQ(s.sliders, 22);
}

TEST(ClipOpenAbortCallers, ClipSwitchRefusedAfterTheFreeReopensTheOldClipIntoItsOwnReceipt)
{
    SessionModel s = twoLoadedClipsShowingA();
    showFileInEditorModel(s, 1, kClipOpenAbortedPrepBusyAfterFree);
    ASSERT_EQ(s.receipts[1], 22);
    ASSERT_EQ(s.receipts[0], 11);
    ASSERT_EQ(s.active, 0);
    ASSERT_EQ(s.loaded, 0); // the freed old clip is opened again
}

TEST(ClipOpenAbortCallers, ShowFilePlanNeverRecursesIntoTheRowThatFailed)
{
    const ShowFileOpenFailurePlan same = showFileOpenFailurePlan(kClipOpenAbortedPrepBusyAfterFree, 1, 1, 1, 2, false);
    ASSERT_FALSE(same.restoreOldActive);
    ASSERT_FALSE(same.reopenOld);
    const ShowFileOpenFailurePlan corrupt = showFileOpenFailurePlan(kMlvErrCorrupted, 1, 0, 1, 2, false);
    ASSERT_TRUE(corrupt.restoreOldActive); // a corrupt B does not pair A's sliders with B either
    ASSERT_TRUE(corrupt.reopenOld);        // unchanged corrupt-file behaviour otherwise
    const ShowFileOpenFailurePlan exportSelection = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 3, 3, false);
    ASSERT_TRUE(exportSelection.restoreOldActive);
    ASSERT_FALSE(exportSelection.reselectOld);
    ASSERT_FALSE(exportSelection.reopenOld);
}

TEST(ClipOpenAbortCallersWiring, ShowFileInEditorRestoresTheOldActiveRowBeforeAnyReopen)
{
    const QString source = mainWindowSource();
    ASSERT_FALSE(source.isEmpty());
    const QString body = bodyBetween(source, QStringLiteral("int MainWindow::showFileInEditor(int row)"),
                                     QStringLiteral("void MainWindow::addClipToExportQueue(int row, QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    const int open = body.indexOf(QStringLiteral("const int openResult = openMlv( GET_CLIP( row )->getPath() );"));
    const int plan = body.indexOf(QStringLiteral("showFileOpenFailurePlan("));
    const int restore = body.indexOf(QStringLiteral("if( plan.restoreOldActive ) SET_ACTIVE_CLIP_IDX( oldActive );"));
    const int reopen = body.indexOf(QStringLiteral("if( plan.reopenOld ) showFileInEditor( oldActive );"));
    ASSERT_TRUE(open >= 0);
    ASSERT_TRUE(plan > open);
    ASSERT_TRUE(restore > plan);
    ASSERT_TRUE(reopen > restore);
    ASSERT_EQ(body.count(QStringLiteral("showFileInEditor( oldActive )")), 1);
}

// (b) openSession with the abort.
TEST(ClipOpenAbortCallers, SessionClipRefusedByPrepTakesNothingFromTheLoadedClip)
{
    const SessionClipOpenFailurePlan refused = sessionClipOpenPlan(kClipOpenAbortedPrepBusy, 0);
    ASSERT_FALSE(refused.applySliders);  // B's settings never land on A
    ASSERT_FALSE(refused.takeThumbnail); // A's frame never becomes B's thumbnail
    ASSERT_FALSE(refused.activateClip);  // B's row does not become active over A
    ASSERT_TRUE(refused.restoreLastOpened);
    const SessionClipOpenFailurePlan opened = sessionClipOpenPlan(kMlvErrNone, 0);
    ASSERT_TRUE(opened.applySliders && opened.takeThumbnail && opened.activateClip);
    ASSERT_FALSE(opened.restoreLastOpened);
    // nothing of this session loaded yet: the reset sliders take the clip's own settings
    const SessionClipOpenFailurePlan first = sessionClipOpenPlan(kClipOpenAbortedPrepBusy, -1);
    ASSERT_TRUE(first.applySliders);
    ASSERT_FALSE(first.takeThumbnail);
    ASSERT_FALSE(first.activateClip);
    ASSERT_FALSE(first.restoreLastOpened);
}

TEST(ClipOpenAbortCallersWiring, OpenSessionGatesSlidersThumbnailAndActiveRowOnTheOpenResult)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::openSession(QString fileNameSession)"),
                                     QStringLiteral("void MainWindow::saveSession(QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("if( ui->actionFastOpen->isChecked() ) openResult = openMlvForPreview( fileName );")));
    ASSERT_TRUE(body.contains(QStringLiteral("else openResult = openMlv( fileName );")));
    const int plan = body.indexOf(QStringLiteral("sessionClipOpenPlan( openResult, lastOpenedRow );"));
    ASSERT_TRUE(plan >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("if( plan.applySliders ) setSliders( SESSION_LAST_CLIP, false );")) > plan);
    ASSERT_TRUE(body.indexOf(QStringLiteral("if( plan.takeThumbnail ) previewPicture( SESSION_CLIP_COUNT - 1 );")) > plan);
    ASSERT_TRUE(body.indexOf(QStringLiteral("else if( plan.restoreLastOpened ) m_pModel->setActiveRow( lastOpenedRow );")) > plan);
    ASSERT_EQ(body.count(QStringLiteral("setSliders( SESSION_LAST_CLIP, false );")), 1);
    ASSERT_EQ(body.count(QStringLiteral("previewPicture( SESSION_CLIP_COUNT - 1 );")), 1);
    ASSERT_EQ(body.count(QStringLiteral("m_pModel->setActiveRow( SESSION_CLIP_COUNT - 1 );")), 1);
    // surfaced, not silent
    ASSERT_TRUE(body.contains(QStringLiteral("if( !prepRefusedClips.isEmpty() )")));
}

// (c) Fast Open .mcraw import refused by the prep drain.
TEST(ClipOpenAbortCallersWiring, McrawCameraMatrixResetRunsOnlyAfterBothGuards)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                     QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    ASSERT_FALSE(body.isEmpty());
    const int guard = body.indexOf(QStringLiteral("if( !playbackPrepQuiescentBeforeClipReplacement( \"clip-open-preview\" ) )"));
    const int backstop = body.indexOf(QStringLiteral("if( !freeActiveMlvObjectAfterLifecycleBarrier( \"clip-open-preview\" ) )"));
    const int adopt = body.indexOf(QStringLiteral("m_pMlvObject = new_MlvObject;"));
    const int combo = body.indexOf(QStringLiteral("ui->comboBoxUseCameraMatrix->setCurrentIndex(0);"));
    const int handler = body.indexOf(QStringLiteral("on_comboBoxUseCameraMatrix_currentIndexChanged(0);"));
    const int processing = body.indexOf(QStringLiteral("setMlvProcessing( m_pMlvObject, m_pProcessingObject );"));
    ASSERT_TRUE(guard >= 0);
    ASSERT_TRUE(backstop > guard);
    ASSERT_TRUE(adopt > backstop);
    ASSERT_TRUE(combo > adopt);       // the retained clip's camera matrix / EXR mode survive a refused open
    ASSERT_TRUE(handler > combo);
    ASSERT_TRUE(processing > handler); // still applied before the new clip is joined to the processing object
    ASSERT_EQ(body.count(QStringLiteral("on_comboBoxUseCameraMatrix_currentIndexChanged(")), 1);
}

TEST(ClipOpenAbortCallersWiring, BothOpenPathsReturnTheRefusalCodesNotACorruptFileCode)
{
    const QString source = mainWindowSource();
    const QString preview = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                        QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    const QString full = bodyBetween(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                     QStringLiteral("//Calculate shutter flavors"));
    ASSERT_FALSE(preview.isEmpty());
    ASSERT_FALSE(full.isEmpty());
    for (const QString & body : {preview, full})
    {
        ASSERT_FALSE(body.contains(QStringLiteral("return MLV_ERR_IO;")));
        ASSERT_EQ(body.count(QStringLiteral("return kClipOpenAbortedPrepBusy;")), 1);
        ASSERT_EQ(body.count(QStringLiteral("return kClipOpenAbortedPrepBusyAfterFree;")), 1);
    }
    ASSERT_TRUE(clipOpenAbortedForPrep(kClipOpenAbortedPrepBusy));
    ASSERT_TRUE(clipOpenAbortedForPrep(kClipOpenAbortedPrepBusyAfterFree));
    for (int mlvErr = 0; mlvErr <= 4; ++mlvErr) ASSERT_FALSE(clipOpenAbortedForPrep(mlvErr)); // enum mlv_err
}

// (d) export with the abort.
TEST(ClipOpenAbortCallers, ExportJobRefusedByPrepIsRetriedThenReportedNeverSilentlyDropped)
{
    for (int retry = 0; retry < kExportOpenAbortMaxRetries; ++retry)
    {
        ASSERT_TRUE(exportOpenFailureAction(kClipOpenAbortedPrepBusy, retry) == ExportOpenFailureAction::RetryJob);
        ASSERT_TRUE(exportOpenFailureAction(kClipOpenAbortedPrepBusyAfterFree, retry) == ExportOpenFailureAction::RetryJob);
    }
    ASSERT_TRUE(exportOpenFailureAction(kClipOpenAbortedPrepBusy, kExportOpenAbortMaxRetries)
                == ExportOpenFailureAction::ReportAndSkipJob);
    ASSERT_TRUE(exportOpenFailureAction(kMlvErrCorrupted, 0) == ExportOpenFailureAction::SkipCorruptJob);
}

TEST(ClipOpenAbortCallersWiring, ExportHandlerKeepsTheRefusedJobAtTheHeadAndRetriesIt)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::exportHandler( void )"),
                                     QStringLiteral("//Export is ready"));
    ASSERT_FALSE(body.isEmpty());
    const int keep = body.indexOf(QStringLiteral("if( m_exportRetryHeadJob ) m_exportRetryHeadJob = false;"));
    const int take = body.indexOf(QStringLiteral("ReceiptSettings *receipt = m_exportQueue.takeFirst();"));
    ASSERT_TRUE(keep >= 0);
    ASSERT_TRUE(take > keep);
    ASSERT_TRUE(body.contains(QStringLiteral("else if( !m_exportQueue.empty() ) //Only to avoid crashes")));
    const int open = body.indexOf(QStringLiteral("const int openResult = openMlv( m_exportQueue.first()->fileName() );"));
    const int decide = body.indexOf(QStringLiteral("switch( exportOpenFailureAction( openResult, m_exportOpenAbortRetries ) )"));
    const int retry = body.indexOf(QStringLiteral("QTimer::singleShot( kExportOpenAbortRetryDelayMs, this, &MainWindow::exportHandler );"));
    const int report = body.indexOf(QStringLiteral("case ExportOpenFailureAction::ReportAndSkipJob:"));
    const int skip = body.indexOf(QStringLiteral("emit exportReady();"));
    ASSERT_TRUE(open >= 0);
    ASSERT_TRUE(decide > open);
    ASSERT_TRUE(retry > decide);
    ASSERT_TRUE(report > retry);
    ASSERT_TRUE(body.mid(retry, report - retry).contains(QStringLiteral("m_exportRetryHeadJob = true;")) ||
                body.mid(decide, retry - decide).contains(QStringLiteral("m_exportRetryHeadJob = true;")));
    ASSERT_TRUE(skip > report);
}

// Class sweep: no caller of openMlv / openMlvForPreview drops its result on the floor.
TEST(ClipOpenAbortCallersWiring, EveryOpenCallerKeepsTheResult)
{
    const QString source = mainWindowSource();
    ASSERT_FALSE(source.isEmpty());
    const QRegularExpression bareCall(QStringLiteral("(^|\\)\\s|else\\s)openMlv(ForPreview)?\\("));
    QStringList offenders;
    const QStringList lines = source.split(QLatin1Char('\n'));
    for (int i = 0; i < lines.size(); ++i)
    {
        const QString line = lines.at(i).trimmed();
        if (line.startsWith(QStringLiteral("//")) || line.startsWith(QStringLiteral("int MainWindow::"))) continue;
        if (bareCall.match(line).hasMatch()) offenders << QStringLiteral("%1: %2").arg(i + 1).arg(line);
    }
    if (!offenders.isEmpty()) fprintf(stderr, "bare open calls:\n%s\n", qPrintable(offenders.join(QLatin1Char('\n'))));
    ASSERT_TRUE(offenders.isEmpty());

    const QString rename = bodyBetween(source, QStringLiteral("void MainWindow::renameActiveClip( void )"),
                                       QStringLiteral("void MainWindow::rightClickShowFile( void )"));
    ASSERT_TRUE(rename.contains(QStringLiteral(
        "if( clipOpenAbortedForPrep( reopenResult ) ) retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );")));
    const QString exportEnd = bodyBetween(source, QStringLiteral("//Open last file which was opened before export"),
                                          QStringLiteral("//Unblock GUI"));
    ASSERT_TRUE(exportEnd.contains(QStringLiteral(
        "if( clipOpenAbortedForPrep( reopenResult ) ) retryClipReopenAfterPrepAbort( m_lastClipBeforeExport );")));
    // the deferred retry runs through showFileInEditor only while that row is still the active one
    ASSERT_TRUE(rename.contains(QStringLiteral(
        "if( row >= 0 && row < SESSION_CLIP_COUNT && row == SESSION_ACTIVE_CLIP_ROW ) showFileInEditor( row );")));
}
