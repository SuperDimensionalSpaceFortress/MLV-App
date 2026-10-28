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
                                     QStringLiteral("bool MainWindow::addClipToExportQueue(int row, QString fileName)"));
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
    // adversary A3: nothing of this session loaded yet. setSliders would run ReceiptSafety against the
    // retained clip and rewrite the XML receipt (Dual ISO valid/Fast -> invalid/off), so it still takes no sliders.
    const SessionClipOpenFailurePlan first = sessionClipOpenPlan(kClipOpenAbortedPrepBusy, -1);
    ASSERT_FALSE(first.applySliders);
    ASSERT_FALSE(first.takeThumbnail);
    ASSERT_FALSE(first.activateClip);
    ASSERT_FALSE(first.restoreLastOpened);
}

TEST(ClipOpenAbortCallersWiring, OpenSessionWithNoClipOpenedReopensTheActiveClipLater)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::openSession(QString fileNameSession)"),
                                     QStringLiteral("void MainWindow::saveSession(QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    const int warn = body.indexOf(QStringLiteral("if( !prepRefusedClips.isEmpty() )"));
    const int retry = body.indexOf(QStringLiteral(
        "if( lastOpenedRow < 0 && SESSION_CLIP_COUNT > 0 ) retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );"));
    ASSERT_TRUE(warn >= 0);
    ASSERT_TRUE(retry > warn);
}

// adversary A1: a refused import must not save the retained clip's sliders into the new clip's receipt
// (deleteFileFromSession -> setReceipt( ACTIVE_RECEIPT )) nor offer the corrupt-file delete dialog.
TEST(ClipOpenAbortCallersWiring, RefusedImportLeavesTheSessionWithoutAReceiptSaveOrDeleteDialog)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::importNewMlv(QString fileName)"),
                                     QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    const int before = body.indexOf(QStringLiteral("const int previousActive = SESSION_CLIP_COUNT > 0 ? SESSION_ACTIVE_CLIP_ROW : -1;"));
    const int add = body.indexOf(QStringLiteral("addFileToSession( fileName );"));
    const int refused = body.indexOf(QStringLiteral("if( clipOpenAbortedForPrep( ret ) )"));
    const int opened = body.indexOf(QStringLiteral("else if( !ret )"));
    const int corrupt = body.indexOf(QStringLiteral("deleteFileFromSession();"));
    ASSERT_TRUE(before >= 0);
    ASSERT_TRUE(add > before);
    ASSERT_TRUE(refused > add);
    ASSERT_TRUE(opened > refused);
    ASSERT_TRUE(corrupt > opened);
    const QString branch = body.mid(refused, opened - refused);
    ASSERT_TRUE(branch.contains(QStringLiteral("m_pModel->removeRow( SESSION_CLIP_COUNT - 1, QModelIndex() );")));
    ASSERT_TRUE(branch.contains(QStringLiteral(
        "if( previousActive >= 0 && previousActive < SESSION_CLIP_COUNT ) SET_ACTIVE_CLIP_IDX( previousActive );")));
    ASSERT_FALSE(branch.contains(QStringLiteral("setReceipt(")));
    ASSERT_FALSE(branch.contains(QStringLiteral("deleteFileFromSession")));
}

// adversary 64a60a72 A1/A2: a refused reopen applies no receipt against the empty / job clip (ReceiptSafety
// rewrites it), and sliders that describe no receipt are never saved into one.
TEST(ClipOpenAbortCallersWiring, SlidersThatDescribeNoReceiptAreNeverSaved)
{
    const QString source = mainWindowSource();
    const QString setReceipt = bodyBetween(source, QStringLiteral("void MainWindow::setReceipt( ReceiptSettings *receipt )"),
                                           QStringLiteral("receipt->setContrast("));
    ASSERT_TRUE(setReceipt.contains(QStringLiteral("if( m_slidersDetachedFromReceipt ) return;")));
    ASSERT_TRUE(setReceipt.indexOf(QStringLiteral("if( m_slidersDetachedFromReceipt ) return;"))
                < setReceipt.indexOf(QStringLiteral("receipt->setExposure(")));
    const QString setSliders = bodyBetween(source, QStringLiteral("void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)"),
                                           QStringLiteral("ui->horizontalSliderExposure->setValue("));
    // r4: setSliders applies nothing while detached; only a successful open re-attaches (markClipOpenSucceeded).
    ASSERT_TRUE(setSliders.contains(QStringLiteral("if( m_slidersDetachedFromReceipt )")));

    const QString exportEnd = bodyBetween(source, QStringLiteral("//Open last file which was opened before export"),
                                          QStringLiteral("//Unblock GUI"));
    ASSERT_TRUE(exportEnd.contains(QStringLiteral(
        "if( reopenResult ) m_slidersDetachedFromReceipt = true;\n"
        "        else setSliders( GET_RECEIPT( m_lastClipBeforeExport ), false );")));
    const QString rename = bodyBetween(source, QStringLiteral("void MainWindow::renameActiveClip( void )"),
                                       QStringLiteral("void MainWindow::retryClipReopenAfterPrepAbort( int row )"));
    ASSERT_TRUE(rename.contains(QStringLiteral(
        "if( !clipOpenAbortedForPrep( reopenResult ) ) setSliders( ACTIVE_RECEIPT, false );")));
    ASSERT_EQ(rename.count(QStringLiteral("setSliders(")), 1);
    const QString session = bodyBetween(source, QStringLiteral("void MainWindow::openSession(QString fileNameSession)"),
                                        QStringLiteral("void MainWindow::saveSession(QString fileName)"));
    ASSERT_TRUE(session.contains(QStringLiteral(
        "else if( plan.restoreLastOpened ) m_pModel->setActiveRow( lastOpenedRow );\n"
        "                        // No clip opened yet: the reset sliders describe no receipt (adversary 64a60a72 A2).\n"
        "                        else m_slidersDetachedFromReceipt = true;")));
}

// adversary A2: the empty object a refused open (or the rename / delete close) leaves behind has no
// processing attached; a receipt save must not dereference it.
TEST(ClipOpenAbortCallersWiring, SetReceiptNeverReadsLevelsFromAnObjectWithoutProcessing)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::setReceipt( ReceiptSettings *receipt )"),
                                     QStringLiteral("void MainWindow::replaceReceipt("));
    ASSERT_FALSE(body.isEmpty());
    const int attached = body.indexOf(QStringLiteral("const bool clipProcessingAttached = m_pMlvObject && m_pMlvObject->processing;"));
    ASSERT_TRUE(attached >= 0);
    ASSERT_TRUE(body.contains(QStringLiteral(
        "if( clipProcessingAttached ) receipt->setDualIsoForced( llrpGetDualIsoValidity( m_pMlvObject ) );")));
    const int guard = body.indexOf(QStringLiteral("    if( clipProcessingAttached )\n    {\n        receipt->setDualIsoWhite("));
    ASSERT_TRUE(guard > attached);
    ASSERT_EQ(body.count(QStringLiteral("m_pMlvObject->processing )")), 2);
    ASSERT_EQ(body.count(QStringLiteral("llrpGetDualIsoValidity(")), 1);
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
    const int retry = body.indexOf(QStringLiteral("QTimer::singleShot( kExportOpenAbortRetryDelayMs, this, [this, batch]()"));
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

// ---- r4: adversary 50fe2263 A1-A4 and Dot B3 -------------------------------------------------------------

namespace
{

// The r4 session model: the sliders carry a detached flag that only a successful open clears, and a receipt
// save (setReceipt / saveSession) is a no-op while it is set.
struct DetachingSessionModel
{
    std::vector<int> receipts;
    int sliders = 0;
    int active = 0;
    bool detached = false;
    bool deleteInProgress = false;
};

void saveSlidersModel(DetachingSessionModel & s)
{
    if (!s.detached) s.receipts[s.active] = s.sliders;
}

// deleteFileFromSession( A ) followed by its showFileInEditor( nearest ) with openMlv returning openResult.
void deleteActiveClipModel(DetachingSessionModel & s, int openResult)
{
    s.deleteInProgress = true;
    s.receipts.erase(s.receipts.begin() + s.active);
    const int nearest = s.active < static_cast<int>(s.receipts.size()) ? s.active : static_cast<int>(s.receipts.size()) - 1;
    s.active = nearest; // SET_ACTIVE_CLIP_IDX( nearest ) before showFileInEditor( nearest )
    const int oldActive = s.active;
    if (openResult)
    {
        const ShowFileOpenFailurePlan plan = showFileOpenFailurePlan(
            openResult, nearest, oldActive, 1, static_cast<int>(s.receipts.size()), false);
        if (plan.restoreOldActive) s.active = oldActive;
        if (plan.detachSliders) s.detached = true;
    }
    else
    {
        s.detached = false; // openMlv succeeded
        s.sliders = s.receipts[nearest];
    }
    s.deleteInProgress = false;
}

} // namespace

// A1: removing A pre-activates B; a refused open of B must not let A's sliders be saved into B.
TEST(ClipOpenAbortR4, RefusedOpenAfterDeletingTheActiveClipNeverSavesItsSlidersIntoTheNextClip)
{
    DetachingSessionModel s;
    s.receipts = {11, 22}; // A exposure 11, B exposure 22
    s.sliders = 11;
    s.active = 0;
    deleteActiveClipModel(s, kClipOpenAbortedPrepBusy);
    ASSERT_EQ(static_cast<int>(s.receipts.size()), 1);
    ASSERT_TRUE(s.detached);
    saveSlidersModel(s); // saveSession -> setReceipt( ACTIVE_RECEIPT )
    ASSERT_EQ(s.receipts[0], 22);

    const ShowFileOpenFailurePlan sameRow = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 0, 0, 1, 1, false);
    ASSERT_TRUE(sameRow.detachSliders);
    ASSERT_TRUE(sameRow.retryReopenLater); // B is opened once more, later, which re-attaches the sliders
}

TEST(ClipOpenAbortR4, SlidersStayAttachedOnlyWhenTheOldClipIsActiveAndStillLoaded)
{
    // prep refusal before the free: A is still loaded and active again -> attached, nothing to retry
    const ShowFileOpenFailurePlan kept = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 1, 2, false);
    ASSERT_TRUE(kept.restoreOldActive);
    ASSERT_FALSE(kept.detachSliders);
    ASSERT_FALSE(kept.retryReopenLater);
    // a corrupt file is detected before the free: A is still loaded -> attached (unchanged behaviour)
    const ShowFileOpenFailurePlan corrupt = showFileOpenFailurePlan(kMlvErrCorrupted, 1, 0, 3, 3, false);
    ASSERT_FALSE(corrupt.detachSliders);
    // refused after the free: an empty object is loaded -> detached; one selection reopens A now
    const ShowFileOpenFailurePlan freedOne = showFileOpenFailurePlan(kClipOpenAbortedPrepBusyAfterFree, 1, 0, 1, 2, false);
    ASSERT_TRUE(freedOne.detachSliders);
    ASSERT_TRUE(freedOne.reopenOld);
    ASSERT_FALSE(freedOne.retryReopenLater);
    // ... and with an export selection nothing reopens now, so it is retried later
    const ShowFileOpenFailurePlan freedMany = showFileOpenFailurePlan(kClipOpenAbortedPrepBusyAfterFree, 1, 0, 3, 3, false);
    ASSERT_TRUE(freedMany.detachSliders);
    ASSERT_TRUE(freedMany.retryReopenLater);
    ASSERT_TRUE(clipOpenFailureKeptRetainedClip(kClipOpenAbortedPrepBusy));
    ASSERT_TRUE(clipOpenFailureKeptRetainedClip(kMlvErrCorrupted));
    ASSERT_FALSE(clipOpenFailureKeptRetainedClip(kClipOpenAbortedPrepBusyAfterFree));
    ASSERT_FALSE(clipOpenFailureKeptRetainedClip(kMlvErrNone));
}

TEST(ClipOpenAbortR4Wiring, ShowFileInEditorDetachesTheSlidersAndRetriesOnceFromThePlan)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("int MainWindow::showFileInEditor(int row)"),
                                     QStringLiteral("bool MainWindow::addClipToExportQueue(int row, QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    const int restore = body.indexOf(QStringLiteral("if( plan.restoreOldActive ) SET_ACTIVE_CLIP_IDX( oldActive );"));
    const int detach = body.indexOf(QStringLiteral("if( plan.detachSliders ) m_slidersDetachedFromReceipt = true;"));
    const int retry = body.indexOf(QStringLiteral(
        "if( plan.retryReopenLater && !m_inClipReopenRetry ) retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );"));
    const int reopen = body.indexOf(QStringLiteral("if( plan.reopenOld ) showFileInEditor( oldActive );"));
    ASSERT_TRUE(restore >= 0);
    ASSERT_TRUE(detach > restore);
    ASSERT_TRUE(retry > detach);
    ASSERT_TRUE(reopen > detach);
}

// A1/A2 class: only a successful open re-attaches the sliders; while detached, setSliders (paste, reset) applies
// nothing, so ReceiptSafety never validates one clip's receipt against another clip.
TEST(ClipOpenAbortR4Wiring, OnlyASuccessfulOpenReattachesTheSliders)
{
    const QString source = mainWindowSource();
    const QString setSliders = bodyBetween(source, QStringLiteral("void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)"),
                                           QStringLiteral("ui->horizontalSliderExposure->setValue("));
    ASSERT_FALSE(setSliders.isEmpty());
    ASSERT_FALSE(setSliders.contains(QStringLiteral("m_slidersDetachedFromReceipt = false;")));
    ASSERT_TRUE(setSliders.contains(QStringLiteral("if( m_slidersDetachedFromReceipt )")));
    // markClipOpenSucceeded and deleteSession (no receipt is left to describe) are the only re-attach points
    ASSERT_EQ(source.count(QStringLiteral("m_slidersDetachedFromReceipt = false;")), 2);
    const QString preview = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                        QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    const QString full = bodyBetween(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                     QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    for (const QString & body : {preview, full})
    {
        ASSERT_FALSE(body.isEmpty());
        ASSERT_EQ(body.count(QStringLiteral("markClipOpenSucceeded();")), 1);
        const int attach = body.indexOf(QStringLiteral("markClipOpenSucceeded();"));
        ASSERT_TRUE(attach > body.indexOf(QStringLiteral("return kClipOpenAbortedPrepBusyAfterFree;")));
        ASSERT_TRUE(attach < body.lastIndexOf(QStringLiteral("return MLV_ERR_NONE;")));
    }
    const QString mark = bodyBetween(source, QStringLiteral("void MainWindow::markClipOpenSucceeded( void )"),
                                     QStringLiteral("void MainWindow::retryClipReopenAfterPrepAbort( int row )"));
    ASSERT_TRUE(mark.contains(QStringLiteral("m_slidersDetachedFromReceipt = false;")));
    ASSERT_TRUE(mark.contains(QStringLiteral("++m_clipOpenGeneration;")));
}

// A2: after a refused end-of-export reopen, paste writes the clipboard into A's receipt but neither re-attaches
// the sliders nor sanitizes A against the job's clip; reset is refused the same way.
TEST(ClipOpenAbortR4Wiring, PasteAndResetWhileDetachedNeverTouchTheLoadedClip)
{
    const QString source = mainWindowSource();
    const QString paste = bodyBetween(source, QStringLiteral("void MainWindow::pasteReceiptFromClipboardTo(int row)"),
                                      QStringLiteral("void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)"));
    ASSERT_FALSE(paste.isEmpty());
    ASSERT_TRUE(paste.contains(QStringLiteral("if( row == SESSION_ACTIVE_CLIP_ROW && !m_slidersDetachedFromReceipt )")));
    const QString reset = bodyBetween(source, QStringLiteral("void MainWindow::on_actionResetReceipt_triggered()"),
                                      QStringLiteral("ReceiptSettings *receipt = new ReceiptSettings(); //default"));
    ASSERT_TRUE(reset.contains(QStringLiteral("if( m_slidersDetachedFromReceipt )")));
    const QString importBody = bodyBetween(source, QStringLiteral("void MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_TRUE(importBody.contains(QStringLiteral(
        "if( !clipOpenFailureKeptRetainedClip( ret ) ) m_slidersDetachedFromReceipt = true;")));
}

// A3: the retry budget is per job: abort, completion and a new batch all reset it, and a retry timer from an
// earlier batch never drives a later one.
TEST(ClipOpenAbortR4Wiring, ExportRetryBudgetIsResetOnAbortCompletionAndNewBatch)
{
    const QString source = mainWindowSource();
    const QString handler = bodyBetween(source, QStringLiteral("void MainWindow::exportHandler( void )"),
                                        QStringLiteral("//Play button pressed"));
    ASSERT_FALSE(handler.isEmpty());
    const QString reset = QStringLiteral("resetExportOpenAbortRetries();");
    const int newBatch = handler.indexOf(QStringLiteral("numberOfJobs = m_exportQueue.size();"));
    const int firstReset = handler.indexOf(reset);
    const int jobs = handler.indexOf(QStringLiteral("//Are there jobs?"));
    ASSERT_TRUE(firstReset > newBatch && firstReset < jobs);
    const int done = handler.indexOf(QStringLiteral("//Else if all planned exports are ready"));
    ASSERT_TRUE(handler.indexOf(reset, done) > done);
    ASSERT_TRUE(handler.contains(QStringLiteral("++m_exportBatchGeneration;")));
    ASSERT_TRUE(handler.contains(QStringLiteral("if( batch == m_exportBatchGeneration ) exportHandler();")));
    const QString abort = bodyBetween(source, QStringLiteral("void MainWindow::exportAbort( void )"),
                                      QStringLiteral("void MainWindow::recordPresentedFrame("));
    ASSERT_TRUE(abort.contains(reset));
    const QString resetBody = bodyBetween(source, QStringLiteral("void MainWindow::resetExportOpenAbortRetries( void )"),
                                          QStringLiteral("void MainWindow::exportAbort( void )"));
    ASSERT_TRUE(resetBody.contains(QStringLiteral("m_exportOpenAbortRetries = 0;")));
    ASSERT_TRUE(resetBody.contains(QStringLiteral("m_exportRetryHeadJob = false;")));
}

// A4: the deferred reopen is suppressed while an export holds a clip and after any other clip opened.
TEST(ClipOpenAbortR4, DeferredReopenIsSuppressedWhileAnExportHoldsAClip)
{
    // rename refused -> retry scheduled for row 0 under generation 7
    ASSERT_TRUE(deferredClipReopenDecision(7, 7, false, 0, 0, 2, 0) == DeferredClipReopen::Run);
    // a CDNG export (or Export Current Frame, or any modal dialog) holds row 0's clip when the timer fires:
    // the reopen does not run (no free of the held clip); it is postponed, never run, while the clip is held
    ASSERT_TRUE(deferredClipReopenDecision(7, 7, true, 0, 0, 2, 0) == DeferredClipReopen::Postpone);
    ASSERT_TRUE(deferredClipReopenDecision(7, 7, true, 0, 0, 2, kClipReopenAbortMaxDeferrals) == DeferredClipReopen::Drop);
    // the export's own open of row 0 bumped the generation: dropped even after the hold ends
    ASSERT_TRUE(deferredClipReopenDecision(7, 8, false, 0, 0, 2, 0) == DeferredClipReopen::Drop);
    ASSERT_TRUE(deferredClipReopenDecision(7, 8, true, 0, 0, 2, 0) == DeferredClipReopen::Drop);
    // row checks of r3 still hold
    ASSERT_TRUE(deferredClipReopenDecision(7, 7, false, 1, 0, 2, 0) == DeferredClipReopen::Drop);
    ASSERT_TRUE(deferredClipReopenDecision(7, 7, false, 2, 2, 2, 0) == DeferredClipReopen::Drop);
}

// adversary 0e65f0cc A1: Export Current Frame holds m_pMlvObject across its dialog loop; the deferred reopen
// treats it (and any modal dialog) as a held clip.
TEST(ClipOpenAbortR4Wiring, ExportCurrentFrameHoldsTheClipAgainstDeferredReopens)
{
    const QString source = mainWindowSource();
    const QString frame = bodyBetween(source, QStringLiteral("void MainWindow::on_actionExportCurrentFrame_triggered()"),
                                      QStringLiteral("void MainWindow::on_checkBoxHighLightReconstruction_toggled(bool checked)"));
    ASSERT_FALSE(frame.isEmpty());
    const int hold = frame.indexOf(QStringLiteral("m_exportInProgress = true;"));
    const int exec = frame.indexOf(QStringLiteral("exportDialog->exec();"));
    const int release = frame.indexOf(QStringLiteral("m_exportInProgress = exportWasInProgress;"));
    ASSERT_TRUE(hold >= 0);
    ASSERT_TRUE(exec > hold);
    ASSERT_TRUE(release > exec);
    const QString schedule = bodyBetween(source, QStringLiteral("void MainWindow::scheduleClipReopenRetry( int row, unsigned long long generation, int deferrals )"),
                                         QStringLiteral("void MainWindow::rightClickShowFile( void )"));
    ASSERT_TRUE(schedule.contains(QStringLiteral(
        "const bool clipHeld = m_exportInProgress || QApplication::activeModalWidget() != nullptr;")));
}

// adversary 0e65f0cc A2: every site that replaces the loaded clip with an empty object detaches the sliders
// (rename close, delete-clip close, both after-free open backstops), so paste never sanitizes against it.
TEST(ClipOpenAbortR4Wiring, EveryEmptyObjectInstallDetachesTheSliders)
{
    const QString source = mainWindowSource();
    const QString rename = bodyBetween(source, QStringLiteral("void MainWindow::renameActiveClip( void )"),
                                       QStringLiteral("//MLV\n    bool ok = QFile( fileName ).rename( newFilePath );"));
    ASSERT_FALSE(rename.isEmpty());
    ASSERT_TRUE(rename.indexOf(QStringLiteral("m_slidersDetachedFromReceipt = true;"))
                > rename.indexOf(QStringLiteral("m_pMlvObject = initMlvObject();")));
    const QString deleteClip = bodyBetween(source, QStringLiteral("freeActiveMlvObjectAfterLifecycleBarrier( \"delete-clip\" );"),
                                           QStringLiteral("#endif"));
    ASSERT_TRUE(deleteClip.contains(QStringLiteral("m_slidersDetachedFromReceipt = true;")));
    const QString preview = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                        QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    const QString full = bodyBetween(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                     QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    for (const QString & body : {preview, full})
    {
        const int empty = body.indexOf(QStringLiteral("m_pMlvObject = initMlvObject();"));
        const int detach = body.indexOf(QStringLiteral("m_slidersDetachedFromReceipt = true;"), empty);
        const int refused = body.indexOf(QStringLiteral("return kClipOpenAbortedPrepBusyAfterFree;"));
        ASSERT_TRUE(empty >= 0);
        ASSERT_TRUE(detach > empty && detach < refused);
    }
}

TEST(ClipOpenAbortR4Wiring, DeferredReopenChecksGenerationAndExportBeforeShowingTheClip)
{
    const QString source = mainWindowSource();
    const QString retry = bodyBetween(source, QStringLiteral("void MainWindow::retryClipReopenAfterPrepAbort( int row )"),
                                      QStringLiteral("void MainWindow::rightClickShowFile( void )"));
    ASSERT_FALSE(retry.isEmpty());
    const int capture = retry.indexOf(QStringLiteral("scheduleClipReopenRetry( row, m_clipOpenGeneration, 0 );"));
    const int gate = retry.indexOf(QStringLiteral(
        "switch( deferredClipReopenDecision( generation, m_clipOpenGeneration, clipHeld, row, SESSION_ACTIVE_CLIP_ROW, SESSION_CLIP_COUNT, deferrals ) )"));
    const int show = retry.indexOf(QStringLiteral("showFileInEditor( row );"));
    ASSERT_TRUE(capture >= 0);
    ASSERT_TRUE(gate > capture);
    ASSERT_TRUE(show > gate);
    ASSERT_EQ(retry.count(QStringLiteral("showFileInEditor(")), 1);
    // the export flag spans the GUI block to the final reopen
    const QString exportStart = bodyBetween(source, QStringLiteral("void MainWindow::on_actionExport_triggered()"),
                                            QStringLiteral("exportHandler();"));
    ASSERT_TRUE(exportStart.indexOf(QStringLiteral("m_exportInProgress = true;"))
                > exportStart.indexOf(QStringLiteral("setEnabled( false );")));
    const QString handler = bodyBetween(source, QStringLiteral("void MainWindow::exportHandler( void )"),
                                        QStringLiteral("//Play button pressed"));
    const int finalReopen = handler.indexOf(QStringLiteral("const int reopenResult = openMlv( GET_RECEIPT( m_lastClipBeforeExport )->fileName() );"));
    const int cleared = handler.indexOf(QStringLiteral("m_exportInProgress = false;"));
    ASSERT_TRUE(finalReopen >= 0);
    ASSERT_TRUE(cleared > finalReopen);
    ASSERT_TRUE(handler.indexOf(QStringLiteral("m_exportInProgress = true;")) >= 0);
    // the other deferred entry that reaches a clip open (macOS FileOpen) is refused during an export
    const QString fileOpen = bodyBetween(source, QStringLiteral("bool MainWindow::event(QEvent *event)"),
                                         QStringLiteral("importNewMlv( fileName );"));
    ASSERT_TRUE(fileOpen.contains(QStringLiteral("if( m_exportInProgress )")));
}

// adversary ddda72d6 A1-A3: while detached, nothing pairs the loaded object with the active row (Export Current
// Frame, Look Assist toggle / baseline), and every failed final reopen or failed import detaches.
TEST(ClipOpenAbortR4Wiring, DetachedSlidersBlockEveryLoadedObjectReaderOfTheActiveRow)
{
    const QString source = mainWindowSource();
    const QString frame = bodyBetween(source, QStringLiteral("void MainWindow::on_actionExportCurrentFrame_triggered()"),
                                      QStringLiteral("SingleFrameExportDialog *exportDialog = new SingleFrameExportDialog("));
    ASSERT_TRUE(frame.contains(QStringLiteral("if( m_slidersDetachedFromReceipt )")));
    const QString capture = bodyBetween(source, QStringLiteral("void MainWindow::captureLookAssistBaseline( ReceiptSettings *receipt )"),
                                        QStringLiteral("void MainWindow::restoreLookAssistBaseline( ReceiptSettings *receipt )"));
    ASSERT_TRUE(capture.contains(QStringLiteral("if( !receipt || m_slidersDetachedFromReceipt ) return;")));
    const QString toggle = bodyBetween(source, QStringLiteral("void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )"),
                                       QStringLiteral("logInteractionEvent("));
    ASSERT_TRUE(toggle.contains(QStringLiteral("if( !m_fileLoaded || !ACTIVE_RECEIPT || m_slidersDetachedFromReceipt ) return;")));
    const QString importBody = bodyBetween(source, QStringLiteral("void MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    const int detach = importBody.lastIndexOf(QStringLiteral("m_slidersDetachedFromReceipt = true;"));
    const int remove = importBody.indexOf(QStringLiteral("deleteFileFromSession();"));
    ASSERT_TRUE(remove >= 0);
    ASSERT_TRUE(detach >= 0 && detach < remove);
    ASSERT_TRUE(importBody.indexOf(QStringLiteral("else if( !ret )")) < detach);
}

// Dot B3: a clip the export queue refused is reported, the single-clip path never reads first() of an empty
// queue, and the mixed-selection path names the refused clip.
TEST(ClipOpenAbortR4Wiring, RefusedQueueConstructionIsReportedAndNeverReadsAnEmptyQueue)
{
    const QString source = mainWindowSource();
    const QString add = bodyBetween(source, QStringLiteral("bool MainWindow::addClipToExportQueue(int row, QString fileName)"),
                                    QStringLiteral("ReceiptSettings *receipt = new ReceiptSettings();"));
    ASSERT_FALSE(add.isEmpty());
    ASSERT_TRUE(add.contains(QStringLiteral("if( showFileInEditor( row ) ) return false;")));
    const QString exportStart = bodyBetween(source, QStringLiteral("void MainWindow::on_actionExport_triggered()"),
                                            QStringLiteral("exportHandler();"));
    ASSERT_TRUE(exportStart.contains(QStringLiteral(
        "if( !addClipToExportQueue( exportRow, fileName ) ) refusedClips.append( GET_CLIP( exportRow )->getName() );")));
    ASSERT_TRUE(exportStart.contains(QStringLiteral(
        "if( !skipFile && !addClipToExportQueue( row, fileName ) ) refusedClips.append( GET_CLIP( row )->getName() );")));
    const int report = exportStart.indexOf(QStringLiteral("if( !refusedClips.isEmpty() )"));
    const int empty = exportStart.indexOf(QStringLiteral("if( m_exportQueue.isEmpty() )"), report);
    const int first = exportStart.indexOf(QStringLiteral("m_exportQueue.first()"));
    ASSERT_TRUE(report >= 0);
    ASSERT_TRUE(empty > report);
    ASSERT_TRUE(first > empty);
    ASSERT_EQ(exportStart.count(QStringLiteral("addClipToExportQueue(")), 2);
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
    // the deferred retry runs through showFileInEditor only while that row is still the active one (r4: and
    // only under the clip-open generation it was scheduled for, outside any export)
    ASSERT_TRUE(rename.contains(QStringLiteral(
        "switch( deferredClipReopenDecision( generation, m_clipOpenGeneration, clipHeld, row, SESSION_ACTIVE_CLIP_ROW, SESSION_CLIP_COUNT, deferrals ) )")));
}
