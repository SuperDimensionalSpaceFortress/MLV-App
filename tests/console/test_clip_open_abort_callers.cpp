// CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 r3 (key3 r1 INV-1/INV-2 = Dot B2/B3, catalogue row CO-01): a clip open
// that a busy playback-prep task refuses must leave the retained clip and every receipt alone, and no caller may
// treat the refusal as a corrupt file. MainWindow cannot be built headless, so each caller is covered twice: its
// decision runs through ClipOpenAbortPolicy.h on a model of the caller's state, and a wiring test pins the real
// caller body in MainWindow.cpp to that decision (each wiring test is red on f5baf5ef).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ClipOpenAbortPolicy.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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
    const QString body = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
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
    ASSERT_TRUE(branch.contains(QStringLiteral("m_pModel->removeRow( importedRow, QModelIndex() );")));
    // r7: previousActive is restored through refusedImportActiveRow (ClipOpenAbortR7 covers its rows)
    ASSERT_TRUE(branch.contains(QStringLiteral(
        "const int restoredActive = refusedImportActiveRow( previousRow, activeBeforeRemoval, importedRow, SESSION_CLIP_COUNT );")));
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

// adversary 80ca1417 A2: with Create MAPP files on, the loader writes the clip's index map; the prep-busy refusal
// runs before any loader call, so a refused open leaves nothing on disk (and needs no free of a new object).
TEST(ClipOpenAbortR5Wiring, BothOpenPathsRefuseBeforeTheLoaderTouchesTheClip)
{
    const QString source = mainWindowSource();
    const QString preview = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                        QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    const QString full = bodyBetween(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                     QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    for (const QString & body : {preview, full})
    {
        ASSERT_FALSE(body.isEmpty());
        const int refused = body.indexOf(QStringLiteral("return kClipOpenAbortedPrepBusy;"));
        const int loader = body.indexOf(QStringLiteral("new_MlvObject = initMlvObjectWith"));
        ASSERT_TRUE(refused >= 0);
        ASSERT_TRUE(loader > refused);
        const int guard = body.lastIndexOf(QStringLiteral("if( !playbackPrepQuiescentBeforeClipReplacement( "), refused);
        ASSERT_TRUE(guard >= 0);
        ASSERT_FALSE(body.mid(guard, refused - guard).contains(QStringLiteral("freeMlvObject(")));
    }
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
            openResult, nearest, oldActive, 1, static_cast<int>(s.receipts.size()), false, false); // A is loaded, not B
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

    const ShowFileOpenFailurePlan sameRow = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 0, 0, 1, 1, false, false);
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
    // r5: the reset guard is the one ownership predicate (attached AND the loaded clip is the active row's)
    ASSERT_TRUE(reset.contains(QStringLiteral("if( !receiptOwnedByLoadedClip( \"reset-receipt\" ) )")));
    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_TRUE(importBody.contains(QStringLiteral(
        "if( !keptPrevious ) m_slidersDetachedFromReceipt = true;")));
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
                                         QStringLiteral("imports.note( importNewMlv( fileName ) );"));
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
    // r5: the toggle guard is the one ownership predicate (attached AND the loaded clip is the active row's)
    ASSERT_TRUE(toggle.contains(QStringLiteral(
        "if( !m_fileLoaded || !ACTIVE_RECEIPT || !receiptOwnedByLoadedClip( \"look-assist-toggle\" ) ) return;")));
    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
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

// ---- r5: Dot 29eac4d5 A1 = adversary 008a3a27 A1 / 4b41aa22 A2 -------------------------------------------------
// After a failed final export reopen the job's clip B stays loaded, the sliders are detached and A is the active
// row again. Writes that go straight into ACTIVE_RECEIPT (around setReceipt) must not carry B's data into A.

namespace
{

struct ReceiptModel
{
    int cutIn = 0;
    int cutOut = 0;
    int dualIsoAutoCorrected = 0;
    int dualIsoPattern = 0;
};

// The MainWindow function whose definition is the nearest one above line index, and that definition's index.
QString enclosingFunction(const QStringList & lines, int index, int * definitionIndex = nullptr)
{
    const QRegularExpression definition(QStringLiteral("^\\S.*MainWindow::(\\w+)\\s*\\("));
    for (int i = index; i >= 0; --i)
    {
        const QRegularExpressionMatch m = definition.match(lines.at(i));
        if (!m.hasMatch()) continue;
        if (definitionIndex) *definitionIndex = i;
        return m.captured(1);
    }
    return QString();
}

} // namespace

// (a) A trim 20-40; B has 100 frames and trim 5-5; Play widens the PLAYED range to 5-100, A's receipt keeps 20-40.
TEST(ClipOpenAbortR5, PlayAfterAFailedFinalReopenNeverWritesTheRetainedClipsRangeIntoTheActiveReceipt)
{
    ReceiptModel a;
    a.cutIn = 20;
    a.cutOut = 40;
    const bool owns = activeReceiptOwnsLoadedClip(true, true, true, false); // detached, B loaded, A active
    ASSERT_FALSE(owns);
    const playback_frame_range::CutRange played = playback_frame_range::normalizeCutRange(5, 5, 100, true);
    ASSERT_TRUE(played.valid);
    ASSERT_TRUE(played.changed);
    ASSERT_EQ(played.cutIn, 5);
    ASSERT_EQ(played.cutOut, 100);
    if (playbackCutRangeRepairWritesReceipt(played.changed, owns))
    {
        a.cutIn = played.cutIn;
        a.cutOut = played.cutOut;
    }
    ASSERT_EQ(a.cutIn, 20);
    ASSERT_EQ(a.cutOut, 40);
    // attached, but the loaded clip is not the active row's (or nothing is loaded): still no write
    ASSERT_FALSE(activeReceiptOwnsLoadedClip(false, true, true, false));
    ASSERT_FALSE(activeReceiptOwnsLoadedClip(false, false, true, true));
    ASSERT_FALSE(activeReceiptOwnsLoadedClip(false, true, false, true));
    // the owning receipt still takes the repaired range (unchanged behaviour)
    ASSERT_TRUE(playbackCutRangeRepairWritesReceipt(true, activeReceiptOwnsLoadedClip(false, true, true, true)));
    ASSERT_FALSE(playbackCutRangeRepairWritesReceipt(false, true));
}

// (b) A has Dual ISO correction 0 and pattern 0; a retained-B frame with the tool on and pattern -2 writes neither.
TEST(ClipOpenAbortR5, DrawFrameReadyNeverWritesARetainedClipsDualIsoCorrectionIntoTheActiveReceipt)
{
    ReceiptModel a;
    const int uiPatternOfB = 2;
    const DualIsoFrameReceiptWrites detached =
        dualIsoFrameReceiptWrites(activeReceiptOwnsLoadedClip(true, true, true, false), 1, -2);
    if (detached.autoCorrected) a.dualIsoAutoCorrected = 1;
    if (detached.pattern) a.dualIsoPattern = uiPatternOfB;
    ASSERT_EQ(a.dualIsoAutoCorrected, 0);
    ASSERT_EQ(a.dualIsoPattern, 0);
    // an owning receipt keeps the base behaviour: the flag always, the pattern only when the core detected one
    const DualIsoFrameReceiptWrites owned = dualIsoFrameReceiptWrites(true, 1, -2);
    ASSERT_TRUE(owned.autoCorrected);
    ASSERT_TRUE(owned.pattern);
    const DualIsoFrameReceiptWrites ownedFixed = dualIsoFrameReceiptWrites(true, 2, 0);
    ASSERT_TRUE(ownedFixed.autoCorrected);
    ASSERT_FALSE(ownedFixed.pattern);
    const DualIsoFrameReceiptWrites toolOff = dualIsoFrameReceiptWrites(true, 0, -2);
    ASSERT_FALSE(toolOff.autoCorrected);
    ASSERT_FALSE(toolOff.pattern);
}

// (a) wiring: Play's repair clamps the spin boxes (what it plays) and writes the receipt only through the predicate.
TEST(ClipOpenAbortR5Wiring, PlayCutRangeRepairWritesTheReceiptOnlyWhenItOwnsTheLoadedClip)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("bool MainWindow::normalizePlaybackCutRangeForLoadedClip( const char *where )"),
                                     QStringLiteral("int MainWindow::normalizePlaybackRequestedFrame("));
    ASSERT_FALSE(body.isEmpty());
    const int clamp = body.indexOf(QStringLiteral("ui->spinBoxCutOut->setValue( normalized.cutOut );"));
    const int gate = body.indexOf(QStringLiteral(
        "if( playbackCutRangeRepairWritesReceipt( normalized.changed, receiptOwnedByLoadedClip( \"play-cut-range-repair\" ) ) )"));
    const int write = body.indexOf(QStringLiteral("ACTIVE_RECEIPT->setCutIn("));
    ASSERT_TRUE(clamp >= 0);
    ASSERT_TRUE(gate > clamp);
    ASSERT_TRUE(write > gate);
    ASSERT_EQ(body.count(QStringLiteral("ACTIVE_RECEIPT->set")), 2);
}

// (b) wiring: drawFrameReady decides both Dual ISO receipt writes through the predicate.
TEST(ClipOpenAbortR5Wiring, DrawFrameReadyWritesDualIsoIntoTheReceiptOnlyWhenItOwnsTheLoadedClip)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::drawFrameReady()"),
                                     QStringLiteral("ui->horizontalSliderDualIsoEvCorrection->setValue("));
    ASSERT_FALSE(body.isEmpty());
    const int decide = body.indexOf(QStringLiteral(
        "dualIsoFrameReceiptWrites( receiptOwnedByLoadedClip( \"draw-frame-dual-iso\" ), dualIsoToolMode, readyFrame.dualIsoPattern );"));
    const int flag = body.indexOf(QStringLiteral("if( receiptWrites.autoCorrected ) ACTIVE_RECEIPT->setDualIsoAutoCorrected( 1 );"));
    const int pattern = body.indexOf(QStringLiteral("if( receiptWrites.pattern ) ACTIVE_RECEIPT->setDualIsoPattern( uiPattern );"));
    ASSERT_TRUE(decide >= 0);
    ASSERT_TRUE(flag > decide);
    ASSERT_TRUE(pattern > flag);
    ASSERT_EQ(body.count(QStringLiteral("ACTIVE_RECEIPT->set")), 2);
}

// (c) class sweep: every direct ACTIVE_RECEIPT / activeReceipt setter sits in a function that consults the one
// predicate before it (or is a listed exemption that writes no loaded-clip data). A new site fails here until it
// is guarded and listed in the run's direct-receipt-writes.md.
TEST(ClipOpenAbortR5Wiring, EveryDirectActiveReceiptWriteConsultsTheOwnershipPredicate)
{
    const QString source = mainWindowSource();
    ASSERT_FALSE(source.isEmpty());
    struct Site { const char *function; const char *guard; };
    const Site guarded[] = {
        {"drawFrameReady", "receiptOwnedByLoadedClip( \"draw-frame-dual-iso\" )"},
        {"normalizePlaybackCutRangeForLoadedClip", "receiptOwnedByLoadedClip( \"play-cut-range-repair\" )"},
        {"applyLookAssistToReceipt", "receiptOwnedByLoadedClip( \"look-assist-async-apply\" )"},
        {"on_actionResetReceipt_triggered", "receiptOwnedByLoadedClip( \"reset-receipt\" )"},
        {"on_comboBoxLookAssistFlavor_currentIndexChanged", "receiptOwnedByLoadedClip( \"look-assist-flavor\" )"},
        {"on_checkBoxLookAssistEnable_clicked", "receiptOwnedByLoadedClip( \"look-assist-toggle\" )"},
    };
    // write no loaded-clip data: the smoke harness forces Look Assist off; a new row's fresh receipt is marked forced
    const QStringList exempt = {QStringLiteral("runGuiPlaybackSmoke"), QStringLiteral("addFileToSession")};

    const QRegularExpression write(QStringLiteral("\\b(ACTIVE_RECEIPT|activeReceipt)\\s*->\\s*set\\w+\\s*\\("));
    const QStringList lines = source.split(QLatin1Char('\n'));
    QStringList offenders;
    QStringList seen;
    for (int i = 0; i < lines.size(); ++i)
    {
        const QString line = lines.at(i).trimmed();
        if (line.startsWith(QStringLiteral("//")) || !write.match(line).hasMatch()) continue;
        int start = 0;
        const QString function = enclosingFunction(lines, i, &start);
        if (!seen.contains(function)) seen << function;
        if (exempt.contains(function)) continue;
        bool ok = false;
        for (const Site & site : guarded)
        {
            if (function != QLatin1String(site.function)) continue;
            // the guard sits between the function's definition and this write
            ok = lines.mid(start, i - start).join(QLatin1Char('\n')).contains(QLatin1String(site.guard));
        }
        if (!ok) offenders << QStringLiteral("%1 (%2): %3").arg(i + 1).arg(function, line);
    }
    if (!offenders.isEmpty()) fprintf(stderr, "unguarded direct receipt writes:\n%s\n", qPrintable(offenders.join(QLatin1Char('\n'))));
    ASSERT_TRUE(offenders.isEmpty());
    // every listed site still exists (the table is not stale)
    for (const Site & site : guarded) ASSERT_TRUE(seen.contains(QLatin1String(site.function)));

    // the deferred Look Assist applies write ACTIVE_RECEIPT through applyLookAssistToReceipt: their gates consult it
    const QString setSliders = bodyBetween(source, QStringLiteral("void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)"),
                                           QStringLiteral("void MainWindow::captureLookAssistBaseline( ReceiptSettings *receipt )"));
    ASSERT_EQ(setSliders.count(QStringLiteral("|| !receiptOwnedByLoadedClip( \"look-assist-setsliders-frame-ready\" )")), 2);
    const QString toggle = bodyBetween(source, QStringLiteral("void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )"),
                                       QStringLiteral("applyLookAssistToReceipt( ACTIVE_RECEIPT, analysisFrame );"));
    ASSERT_TRUE(toggle.contains(QStringLiteral("|| !receiptOwnedByLoadedClip( \"look-assist-toggle-frame-ready\" )")));

    // the predicate reuses the r4 detachment state and compares the loaded file with the active row
    const QString predicate = bodyBetween(source, QStringLiteral("bool MainWindow::receiptOwnedByLoadedClip( const char *site )"),
                                          QStringLiteral("void MainWindow::retryClipReopenAfterPrepAbort( int row )"));
    ASSERT_TRUE(predicate.contains(QStringLiteral("activeReceiptOwnsLoadedClip( m_slidersDetachedFromReceipt,")));
    ASSERT_TRUE(predicate.contains(QStringLiteral("m_loadedClipFileName == ACTIVE_CLIP->getPath()")));
    const QString preview = bodyBetween(source, QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"),
                                        QStringLiteral("void MainWindow::on_actionOpen_triggered()"));
    const QString full = bodyBetween(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                     QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    for (const QString & body : {preview, full})
        ASSERT_TRUE(body.contains(QStringLiteral("m_loadedClipFileName = fileName;\n    markClipOpenSucceeded();")));
}

// adversary 4b41aa22 A1: session [A,B], A active and loaded; drop C, whose prep drain times out. importNewMlv
// removes C and restores A; the drop handler must not then open B (the last row), which would free A.
TEST(ClipOpenAbortR5, APrepRefusedImportLeavesTheRestoredClipShown)
{
    ImportBatchOutcome onlyRefused;
    onlyRefused.note(false);
    ASSERT_FALSE(onlyRefused.showLastRow());
    // nothing imported (duplicates skipped) or an ordinary failure: the base shows the last row as before
    ImportBatchOutcome none;
    ASSERT_TRUE(none.showLastRow());
    ImportBatchOutcome corrupt;
    corrupt.note(true);
    ASSERT_TRUE(corrupt.showLastRow());
    // a refusal next to another import still shows the last row (that open makes any deferred reopen stale)
    ImportBatchOutcome mixed;
    mixed.note(false);
    mixed.note(true);
    ASSERT_TRUE(mixed.showLastRow());
}

TEST(ClipOpenAbortR5Wiring, EveryImportCallerShowsTheLastRowOnlyFromTheBatchOutcome)
{
    const QString source = mainWindowSource();
    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_FALSE(importBody.isEmpty());
    const int warning = importBody.indexOf(QStringLiteral("was not imported because playback was still preparing a frame"));
    const int refused = importBody.indexOf(QStringLiteral("return false;"));
    ASSERT_TRUE(warning >= 0);
    ASSERT_TRUE(refused > warning);
    ASSERT_TRUE(importBody.contains(QStringLiteral(
        "if( m_slidersDetachedFromReceipt && SESSION_CLIP_COUNT > 0 ) retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );")));
    // r6 (adversary e11815a5 A1): a FileOpen inside this import's event pumps can append and show another clip;
    // the refused clip's own row is removed, and the sliders stay attached only while previousActive is loaded
    const int lookup = importBody.indexOf(QStringLiteral("if( GET_CLIP( i ) == importedClip ) { importedRow = i; break; }"));
    const int removed = importBody.indexOf(QStringLiteral("m_pModel->removeRow( importedRow, QModelIndex() );"));
    ASSERT_TRUE(lookup >= 0 && removed > lookup);
    ASSERT_TRUE(importBody.contains(QStringLiteral(
        "const bool keptPrevious = clipOpenFailureKeptRetainedClip( ret ) && loadedClipIsRow( previousRow );")));
    ASSERT_TRUE(importBody.contains(QStringLiteral("    }\n    return true;\n}")));

    const QString show = QStringLiteral("if( SESSION_CLIP_COUNT && imports.showLastRow() ) showFileInEditor( SESSION_CLIP_COUNT - 1 );");
    struct Caller { const char *start; const char *end; };
    const Caller callers[] = {
        {"bool MainWindow::event(QEvent *event)", "//Caching is in which state? Set it!"},
        {"void MainWindow::on_actionOpen_triggered()", "void MainWindow::openDngFolderDialog()"},
        {"void MainWindow::openDngFolderDialog()", "void MainWindow::on_actionFcpxmlImportAssistant_triggered()"},
        // r6 (adversary 80ca1417 / eb2146f4 A1): the FCPXML import assistant is the fifth caller
        {"void MainWindow::on_actionFcpxmlImportAssistant_triggered()", "void MainWindow::on_actionFcpxmlSelectionAssistant_triggered()"},
    };
    for (const Caller & caller : callers)
    {
        const QString body = bodyBetween(source, QLatin1String(caller.start), QLatin1String(caller.end));
        ASSERT_FALSE(body.isEmpty());
        const int note = body.indexOf(QStringLiteral("imports.note( importNewMlv( "));
        const int shown = body.indexOf(show);
        ASSERT_TRUE(note >= 0);
        ASSERT_TRUE(shown > note);
    }
    // the drop path (openMlvSet) shows the last row from the same outcome
    const int dropShow = source.indexOf(QStringLiteral("if( SESSION_CLIP_COUNT && imports.showLastRow() )\n    {\n        //Show last imported file"));
    const int dropNote = source.lastIndexOf(QStringLiteral("imports.note( importNewMlv( fileName ) );"), dropShow);
    ASSERT_TRUE(dropShow >= 0);
    ASSERT_TRUE(dropNote >= 0 && dropNote < dropShow);
}

// sol r6 CO-ACTIVE-ROW-EMPTY-NESTED: an empty session imports B; inside addFileToSession's processEvents a FileOpen
// imports and shows C (row 1, active 1). B's open is refused and B's row 0 is removed. The stored active row must
// follow C to row 0: Save Session reads ACTIVE_RECEIPT, and the deferred retry must not drop an out-of-range row.
TEST(ClipOpenAbortR7, ARefusedImportIntoAnEmptySessionKeepsTheNestedClipActive)
{
    std::vector<int> receipts = {7, 9}; // B (refused), C (nested import, shown)
    const int previousActive = -1;      // the session was empty when B's import started
    const int activeBeforeRemoval = 1;  // C
    const int removedRow = 0;           // B's own row
    receipts.erase(receipts.begin() + removedRow);
    const int count = static_cast<int>(receipts.size());
    const int active = refusedImportActiveRow(previousActive, activeBeforeRemoval, removedRow, count);
    ASSERT_EQ(active, 0);
    ASSERT_EQ(receipts.at(static_cast<size_t>(active)), 9); // Save Session reads C's receipt
    ASSERT_TRUE(deferredClipReopenDecision(5, 5, false, active, active, count, 0) == DeferredClipReopen::Run);

    // the sol r6 INV-9 rows: previousActive survives and is active again; an emptied session is cleared
    ASSERT_EQ(refusedImportActiveRow(0, 1, 1, 1), 0);  // [A,B], previous A
    ASSERT_EQ(refusedImportActiveRow(-1, 0, 0, 0), -1); // [B]
    ASSERT_EQ(refusedImportActiveRow(0, 2, 1, 2), 0);  // [A,B,C], previous A, C active
    // more nested imports: the active clip keeps its identity across the removal
    ASSERT_EQ(refusedImportActiveRow(-1, 2, 0, 2), 1);  // [B,C,D], D active
    ASSERT_EQ(refusedImportActiveRow(-1, 0, 1, 2), 0);  // an active row before the removed one keeps its index
    // the removed row itself was active: the last surviving row (the clip a nested import showed last)
    ASSERT_EQ(refusedImportActiveRow(-1, 0, 0, 2), 1);

    // whatever the input, the result names a row of the session, and only an empty session gets -1
    for (int n = 1; n <= 4; ++n)
        for (int removed = 0; removed < n; ++removed)
            for (int prev = -1; prev <= n; ++prev)
                for (int before = -1; before <= n; ++before)
                {
                    const int row = refusedImportActiveRow(prev, before, removed, n - 1);
                    if (n - 1 == 0) ASSERT_EQ(row, -1);
                    else ASSERT_TRUE(row >= 0 && row < n - 1);
                }
}

TEST(ClipOpenAbortR7Wiring, RefusedImportSetsTheActiveRowFromThePolicyAfterTheRemoval)
{
    const QString source = mainWindowSource();
    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_FALSE(importBody.isEmpty());
    const int before = importBody.indexOf(QStringLiteral("const int activeBeforeRemoval = SESSION_ACTIVE_CLIP_ROW;"));
    const int removed = importBody.indexOf(QStringLiteral("m_pModel->removeRow( importedRow, QModelIndex() );"));
    const int policy = importBody.indexOf(QStringLiteral(
        "const int restoredActive = refusedImportActiveRow( previousRow, activeBeforeRemoval, importedRow, SESSION_CLIP_COUNT );"));
    const int applied = importBody.indexOf(QStringLiteral(
        "if( restoredActive >= 0 ) SET_ACTIVE_CLIP_IDX( restoredActive );\n            else deleteSession();"));
    const int retry = importBody.indexOf(QStringLiteral("retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );"));
    ASSERT_TRUE(before >= 0);
    ASSERT_TRUE(removed > before);
    ASSERT_TRUE(policy > removed);
    ASSERT_TRUE(applied > policy);
    ASSERT_TRUE(retry > applied);
    ASSERT_FALSE(importBody.contains(QStringLiteral("else if( SESSION_CLIP_COUNT == 0 ) deleteSession();")));
}

// adversary 7fd2d2f1 A1: session [A], A loaded and attached. Import B; inside addFileToSession's processEvents a
// duplicate FileOpen( A ) shows the last row B, whose open is refused (B never loaded: the sliders detach, a retry is
// scheduled for row 1). The outer refusal removes B and restores A while A is still loaded. The sliders are still
// detached, so A must be reopened later; otherwise every later edit of A is dropped by setReceipt.
TEST(ClipOpenAbortR7, ANestedRefusalThatDetachedTheSlidersReopensTheRestoredClipLater)
{
    // showFileInEditor's sibling: the old row is restored and still loaded, but an open inside openMlv's event pump
    // already detached the sliders
    const ShowFileOpenFailurePlan nested = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 1, 2, false, true, true);
    ASSERT_FALSE(nested.detachSliders);
    ASSERT_TRUE(nested.retryReopenLater);
    // unchanged without a nested detach, and an ordinary failure never schedules a reopen
    ASSERT_FALSE(showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 1, 2, false, true, false).retryReopenLater);
    ASSERT_FALSE(showFileOpenFailurePlan(kMlvErrCorrupted, 1, 0, 1, 2, false, true, true).retryReopenLater);
    // a plan that reopens the old clip now needs no later reopen
    ASSERT_FALSE(showFileOpenFailurePlan(kClipOpenAbortedPrepBusyAfterFree, 1, 0, 1, 2, false, false, true).retryReopenLater);
}

// adversary 7fd2d2f1 A2: inside addFileToSession( B )'s event pump a FileOpen opens a session file; that clears A
// and B. B's open is refused, but its row is gone: nothing is removed and no index of the old session is used.
TEST(ClipOpenAbortR7, ARefusedImportWhoseRowIsGoneRemovesNothing)
{
    ASSERT_EQ(refusedImportActiveRow(-1, -1, -1, 0), -1); // the nested session is empty: deleteSession
    ASSERT_EQ(refusedImportActiveRow(-1, 1, -1, 2), 1);   // the nested session [X,Y], Y active: kept
    ASSERT_EQ(refusedImportActiveRow(-1, 5, -1, 2), 1);   // a stale active row: the last row
    ASSERT_EQ(refusedImportActiveRow(0, 1, -1, 2), 0);    // the previous clip, found again by path at row 0
    for (int n = 0; n <= 3; ++n)
        for (int before = -1; before <= n; ++before)
        {
            const int row = refusedImportActiveRow(-1, before, -1, n);
            if (n == 0) ASSERT_EQ(row, -1);
            else ASSERT_TRUE(row >= 0 && row < n);
            if (n > 0 && before >= 0 && before < n) ASSERT_EQ(row, before);
        }
}

TEST(ClipOpenAbortR7Wiring, RefusedImportFindsItsRowAndThePreviousClipByPathAndRetriesWhileDetached)
{
    const QString source = mainWindowSource();
    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_FALSE(importBody.isEmpty());
    const int path = importBody.indexOf(QStringLiteral("ClipInformation *previousClip ="));
    const int add = importBody.indexOf(QStringLiteral("addFileToSession( fileName );"));
    ASSERT_TRUE(path >= 0 && add > path);
    ASSERT_TRUE(importBody.contains(QStringLiteral("int importedRow = -1;")));
    ASSERT_FALSE(importBody.contains(QStringLiteral("int importedRow = SESSION_CLIP_COUNT - 1;")));
    const int removed = importBody.indexOf(QStringLiteral("if( importedRow >= 0 ) m_pModel->removeRow( importedRow, QModelIndex() );"));
    const int found = importBody.indexOf(QStringLiteral("if( GET_CLIP( i ) == previousClip ) { previousRow = i; break; }"));
    const int kept = importBody.indexOf(QStringLiteral(
        "const bool keptPrevious = clipOpenFailureKeptRetainedClip( ret ) && loadedClipIsRow( previousRow );"));
    const int retry = importBody.indexOf(QStringLiteral(
        "if( m_slidersDetachedFromReceipt && SESSION_CLIP_COUNT > 0 ) retryClipReopenAfterPrepAbort( SESSION_ACTIVE_CLIP_ROW );"));
    ASSERT_TRUE(removed >= 0);
    ASSERT_TRUE(found > removed);
    ASSERT_TRUE(kept > found);
    ASSERT_TRUE(retry > kept);

    const QString show = bodyBetween(source, QStringLiteral("int MainWindow::showFileInEditor(int row)"),
                                     QStringLiteral("bool MainWindow::addClipToExportQueue(int row, QString fileName)"));
    ASSERT_TRUE(show.contains(QStringLiteral("oldClipStillLoaded, m_slidersDetachedFromReceipt );")));

    // the delete loop (sibling): a row that is no longer in the session is never read or removed
    const QString del = bodyBetween(source, QStringLiteral("void MainWindow::deleteFileFromSession( void )"),
                                    QStringLiteral("//if there is at least one..."));
    ASSERT_FALSE(del.isEmpty());
    const QString guard = QStringLiteral("if( row < 0 || row >= SESSION_CLIP_COUNT ) continue;");
    const int rowRead = del.indexOf(QStringLiteral("int row = list.at( i - 1 ).data( ROLE_REALINDEX ).toInt();"));
    const int firstGuard = del.indexOf(guard, rowRead);
    const int trash = del.indexOf(QStringLiteral("MoveToTrash( clipFileName )"));
    const int secondGuard = del.indexOf(QStringLiteral("if( row < 0 ) continue;"), trash);
    const int delRemove = del.indexOf(QStringLiteral("m_pModel->removeRow( row, QModelIndex() );"));
    ASSERT_TRUE(rowRead >= 0);
    ASSERT_TRUE(firstGuard > rowRead && trash > firstGuard);
    ASSERT_TRUE(secondGuard > trash && delRemove > secondGuard);
}

// adversary a30fd547 A1: inside addFileToSession( B )'s event pump a FileOpen opens replacement.masxml, which clears
// the session and adds a new B (same path, its own receipt). The refused import must remove only the B object it
// appended, so both rows are found by object, never by path; the delete loop re-finds its clip the same way.
TEST(ClipOpenAbortR7Wiring, RefusedImportAndTheDeleteLoopFindTheirClipsByObject)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("ClipInformation *MainWindow::addFileToSession(QString fileName)")));
    const QString add = bodyBetween(source, QStringLiteral("ClipInformation *MainWindow::addFileToSession(QString fileName)"),
                                    QStringLiteral("int MainWindow::askToSaveCurrentSession()"));
    ASSERT_FALSE(add.isEmpty());
    const int appended = add.indexOf(QStringLiteral("m_pModel->append( clipInfo );"));
    const int pumped = add.indexOf(QStringLiteral("qApp->processEvents();"));
    const int returned = add.indexOf(QStringLiteral("return clipInfo;"));
    ASSERT_TRUE(appended >= 0 && pumped > appended && returned > pumped);

    const QString importBody = bodyBetween(source, QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"),
                                           QStringLiteral("int MainWindow::openMlvForPreview(QString fileName)"));
    ASSERT_FALSE(importBody.isEmpty());
    ASSERT_TRUE(importBody.contains(QStringLiteral("ClipInformation *importedClip = addFileToSession( fileName );")));
    ASSERT_TRUE(importBody.contains(QStringLiteral("if( GET_CLIP( i ) == importedClip ) { importedRow = i; break; }")));
    ASSERT_TRUE(importBody.contains(QStringLiteral("if( GET_CLIP( i ) == previousClip ) { previousRow = i; break; }")));
    ASSERT_FALSE(importBody.contains(QStringLiteral("GET_CLIP( i )->getPath() == fileName")));
    ASSERT_FALSE(importBody.contains(QStringLiteral("previousActivePath")));

    const QString del = bodyBetween(source, QStringLiteral("void MainWindow::deleteFileFromSession( void )"),
                                    QStringLiteral("//if there is at least one..."));
    ASSERT_FALSE(del.isEmpty());
    const int captured = del.indexOf(QStringLiteral("ClipInformation *clip = GET_CLIP( row );"));
    const int name = del.indexOf(QStringLiteral("const QString clipFileName = GET_RECEIPT( row )->fileName();"));
    const int trash = del.indexOf(QStringLiteral("MoveToTrash( clipFileName )"));
    const int refind = del.indexOf(QStringLiteral("if( GET_CLIP( k ) == clip ) { row = k; break; }"));
    const int removed = del.indexOf(QStringLiteral("m_pModel->removeRow( row, QModelIndex() );"));
    ASSERT_TRUE(captured >= 0 && name > captured && trash > name);
    ASSERT_TRUE(refind > trash && removed > refind);
    ASSERT_FALSE(del.contains(QStringLiteral("GET_RECEIPT(row)->fileName()")));
}

// adversary 7354a575 A1: session [A,B,C], A active and loaded with exposure 20, B never loaded and corrupt.
// showFileInEditor( B ) fails; while openMlv's error dialog runs its event loop, a FileOpen imports and shows C
// (exposure 0). The failure plan restores A and reopens it, and that reopen must not save C's sliders into A.
TEST(ClipOpenAbortR6, AClipShownDuringAFailedOpensDialogIsNeverSavedIntoTheRestoredRow)
{
    DetachingSessionModel s;
    s.receipts = {20, 0, 0}; // A, B (never loaded), C (default receipt)
    std::vector<bool> neverLoaded = {false, true, true};
    s.sliders = 20;
    s.active = 0;
    int loaded = 0;
    auto save = [&]() { if (!neverLoaded[s.active]) saveSlidersModel(s); };
    auto openOk = [&](int row) { loaded = row; neverLoaded[row] = false; s.detached = false; s.sliders = s.receipts[row]; };

    save();                      // showFileInEditor( B ): save A's sliders into A
    const int oldActive = s.active;
    s.active = 1;                // SET_ACTIVE_CLIP_IDX( B ), then openMlv( B ) shows its error dialog ...
    save();                      // ... where FileOpen -> showFileInEditor( C ): B was never loaded, nothing saved
    s.active = 2;
    openOk(2);                   // C opens and is shown
    const ShowFileOpenFailurePlan plan = showFileOpenFailurePlan(
        kMlvErrCorrupted, 1, oldActive, 1, 3, false, loaded == oldActive);
    if (plan.restoreOldActive) s.active = oldActive;
    if (plan.detachSliders) s.detached = true;
    if (plan.reopenOld)
    {
        save();                  // showFileInEditor( A ) saves the sliders into the active row first
        s.active = oldActive;
        openOk(oldActive);
    }
    ASSERT_EQ(s.receipts[0], 20); // A keeps its grade
    ASSERT_EQ(s.receipts[2], 0);
    ASSERT_EQ(s.active, 0);
    ASSERT_EQ(loaded, 0);
    ASSERT_EQ(s.sliders, 20);
    // without a re-entrant open the plan is unchanged: A is still loaded, so the sliders stay attached
    ASSERT_FALSE(showFileOpenFailurePlan(kMlvErrCorrupted, 1, 0, 1, 3, false, true).detachSliders);
    ASSERT_FALSE(showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 1, 3, false, true).detachSliders);
    // a prep refusal with another clip loaded detaches and retries A later
    const ShowFileOpenFailurePlan prep = showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 1, 0, 1, 3, false, false);
    ASSERT_TRUE(prep.detachSliders);
    ASSERT_TRUE(prep.retryReopenLater);
}

// adversary e11815a5 A2: the active row A is opened again and fails with an ordinary error that keeps A loaded
// (oldActive == row). The sliders still describe A, so a later edit is saved into A's receipt.
TEST(ClipOpenAbortR6, ASameRowFailureThatKeepsTheClipLoadedLeavesTheSlidersAttached)
{
    const ShowFileOpenFailurePlan kept = showFileOpenFailurePlan(kMlvErrCorrupted, 0, 0, 1, 1, false, true);
    ASSERT_FALSE(kept.detachSliders);
    ASSERT_FALSE(kept.restoreOldActive);
    ASSERT_FALSE(kept.reopenOld);
    ASSERT_FALSE(showFileOpenFailurePlan(kClipOpenAbortedPrepBusy, 0, 0, 1, 1, false, true).detachSliders);
    // the delete path (the loaded clip is the removed one) and a freed clip still detach
    ASSERT_TRUE(showFileOpenFailurePlan(kMlvErrCorrupted, 0, 0, 1, 1, false, false).detachSliders);
    ASSERT_TRUE(showFileOpenFailurePlan(kClipOpenAbortedPrepBusyAfterFree, 0, 0, 1, 1, false, true).detachSliders);
    // a never-loaded row has no receipt the sliders describe
    ASSERT_TRUE(showFileOpenFailurePlan(kMlvErrCorrupted, 0, 0, 1, 1, true, true).detachSliders);
}

TEST(ClipOpenAbortR6Wiring, ShowFileInEditorTellsThePlanWhetherTheOldClipIsStillLoaded)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("int MainWindow::showFileInEditor(int row)"),
                                     QStringLiteral("bool MainWindow::addClipToExportQueue(int row, QString fileName)"));
    ASSERT_FALSE(body.isEmpty());
    const int open = body.indexOf(QStringLiteral("const int openResult = openMlv( GET_CLIP( row )->getPath() );"));
    const int loadedIsOld = body.indexOf(QStringLiteral("const bool oldClipStillLoaded = loadedClipIsRow( oldActive );"));
    const int plan = body.indexOf(QStringLiteral("showFileOpenFailurePlan("));
    ASSERT_TRUE(open >= 0);
    ASSERT_TRUE(loadedIsOld > open);   // read after openMlv returns: a re-entrant open has already run
    ASSERT_TRUE(plan > loadedIsOld);
    ASSERT_TRUE(body.mid(plan, 400).contains(QStringLiteral("oldClipStillLoaded, m_slidersDetachedFromReceipt );")));
}

// adversary 7354a575 A2: the Play-stop drain waits for the prep worker (up to 2 s). The smoke telemetry samples
// its end time first, so the summary measures the Play interval, and the drain still precedes the core mutations.
TEST(ClipOpenAbortR6Wiring, PlayStopDrainsThePrepWorkerAfterTheTelemetryEndpoint)
{
    const QString source = mainWindowSource();
    const QString body = bodyBetween(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
                                     QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(body.isEmpty());
    const QString drain = QStringLiteral("drainPlaybackPrepInFlight( \"play-stop\", 2000 );");
    ASSERT_EQ(body.count(drain), 1);
    const int stopBlock = body.indexOf(QStringLiteral("if( !checked )\n    {"));
    const int finish = body.indexOf(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"), stopBlock);
    const int drained = body.indexOf(drain);
    const int debayer = body.indexOf(QStringLiteral("selectDebayerAlgorithm();"), stopBlock);
    const int dualIso = body.indexOf(QStringLiteral("applyEffectiveDualIsoPlaybackSettings();"), stopBlock);
    ASSERT_TRUE(stopBlock >= 0);
    ASSERT_TRUE(finish > stopBlock);
    ASSERT_TRUE(drained > finish);
    ASSERT_TRUE(debayer > drained);
    ASSERT_TRUE(dualIso > drained);
}
