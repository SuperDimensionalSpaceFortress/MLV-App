// LOOK-FULLSCREEN-PARITY-1: the "current look" label. Owner, 2026-10-15: "Full screen dont bypass the look. I just
// want text in the bottom right showing current look when in full screen and somewhere on GUI when windowed".
// The text, the corner placement and the hide setting are pure (LookLabelPolicy.h); the MainWindow / surface call
// sites are GUI/GL code not linked into console_tests, so they are pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ClipOpenAbortPolicy.h"
#include "../../platform/qt/LookLabelPolicy.h"
#include "../../src/batch/LookAssistAnalysis.h"

#include <QFile>
#include <QRegularExpression>
#include <QSettings>
#include <QString>
#include <QTextStream>
#include <QVariant>

#include <memory>
#include <string>

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

std::string labelText(bool on, const char *flavor)
{
    return look_label::text(on, QString::fromLatin1(flavor)).toStdString();
}

// The body of one function, from its definition to its closing brace in column 0.
QString functionBody(const QString &source, const QString &signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    if ( start < 0 ) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    return source.mid(start, end > start ? end - start : -1);
}

} // namespace

TEST(LookLabelPolicy, TextPerFlavor)
{
    ASSERT_EQ(std::string("Look: Classic"), labelText(true, "classic"));
    ASSERT_EQ(std::string("Look: Cinematic"), labelText(true, "cinematic"));
    ASSERT_EQ(std::string("Look: Film grade"), labelText(true, "film"));
    // The owner-ruled label for the Kodak Vision3 50D flavor (machine spelling vision50d).
    ASSERT_EQ(std::string("Look: Kodak Vision3 50D"), labelText(true, "vision50d"));
    // The flavor name is matched trimmed and case-insensitively, like lookAssistSelectFlavor.
    ASSERT_EQ(std::string("Look: Cinematic"), labelText(true, " Cinematic "));
}

TEST(LookLabelPolicy, OffWhenLookAssistIsOffWhateverTheFlavor)
{
    ASSERT_EQ(std::string("Look: Off"), labelText(false, "classic"));
    ASSERT_EQ(std::string("Look: Off"), labelText(false, "cinematic"));
    ASSERT_EQ(std::string("Look: Off"), labelText(false, "vision50d"));
    ASSERT_EQ(std::string("Look: Off"), labelText(true, ""));
}

TEST(LookLabelPolicy, UnknownFlavorIsShownAsItIs)
{
    ASSERT_EQ(std::string("Look: mystery"), labelText(true, "mystery"));
}

// Catalogue L-04 (sol r4): Cinematic selected + Look Assist on + safety fallback -> the label names the baseline the
// screen shows, never "Cinematic". Built the way updateLookLabel builds it, from the applied outcome.
static std::string appliedLabel(bool on, const lookassist::LookAssistFlavorOutcome &outcome, const void *receipt,
                         const char *selected)
{
    const QString sel = QString::fromLatin1(selected);
    return look_label::appliedText(on, outcome.receiptValue(receipt, sel), outcome.fellBack(receipt)).toStdString();
}

TEST(LookLabelPolicy, L04SafetyFallbackNamesTheBaselineNotTheSelectedFlavor)
{
    int clip = 0, otherClip = 0;
    lookassist::LookAssistFlavorOutcome outcome;
    // Pending (no analysis landed yet): the selector's flavor, which is the clip's setting.
    outcome.begin();
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, &clip, "cinematic"));
    // The analysis fell back: the baseline sliders are on screen.
    outcome.landed(&clip, QStringLiteral("cinematic"), true);
    const std::string fellBack = appliedLabel(true, outcome, &clip, "cinematic");
    ASSERT_EQ(std::string("Look: Baseline (fallback)"), fellBack);
    ASSERT_TRUE(fellBack.find("Cinematic") == std::string::npos);
    // Look Assist off wins; another clip is not this clip's fallback.
    ASSERT_EQ(std::string("Look: Off"), appliedLabel(false, outcome, &clip, "cinematic"));
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, &otherClip, "cinematic"));
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, nullptr, "cinematic"));
    // Applied: the flavor the analysis applied.
    outcome.landed(&clip, QStringLiteral("cinematic"), false);
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, &clip, "cinematic"));
    // Cleared (Look Assist off / flavor changed): back to the selector.
    outcome.landed(&clip, QStringLiteral("cinematic"), true);
    outcome.clear();
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, &clip, "cinematic"));
}

TEST(LookLabelWiring, L04LabelReadsTheAppliedOutcomeAndRefreshesWhenAnalysisLands)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_TRUE(update.contains(QStringLiteral("look_label::shownText(\n        lookOn, m_lookAssistFlavorOutcome, lookReceipt,")));
    ASSERT_FALSE(update.contains(QStringLiteral("look_label::appliedText(")));
    ASSERT_FALSE(update.contains(QStringLiteral("look_label::text(")));

    // Every place an analysis lands (applied or safety fallback) re-derives the label before it logs or leaves:
    // an updateLookLabel() call after each landed( ... ) and before the next logInteractionEvent( or return.
    const QString landedCall = QStringLiteral("m_lookAssistFlavorOutcome.landed(");
    const QRegularExpression leaves(QStringLiteral("logInteractionEvent\\(|\\breturn\\b"));
    int landedSites = 0;
    for ( int at = mw.indexOf(landedCall); at >= 0; at = mw.indexOf(landedCall, at + 1) )
    {
        ++landedSites;
        const int bound = leaves.match(mw, at).capturedStart();
        ASSERT_TRUE(bound > at);
        const int refresh = mw.indexOf(QStringLiteral("updateLookLabel();"), at);
        ASSERT_TRUE(refresh > at && refresh < bound);
    }
    ASSERT_EQ(5, landedSites);   // async fail-closed, async post-measure fallback, async applied, sync fallback, sync applied
}

// CoS r5: begin() forgets the last outcome, so a label re-derived while the analysis is pending would name the
// selected flavor while the previous look (here the fallback baseline) is still on screen. The label is therefore
// deliberately left as it is until the analysis lands.
TEST(LookLabelPolicy, PendingAnalysisKeepsThePreviousLabelUntilItLands)
{
    int clip = 0;
    lookassist::LookAssistFlavorOutcome outcome;
    outcome.landed(&clip, QStringLiteral("cinematic"), true);
    ASSERT_EQ(std::string("Look: Baseline (fallback)"), appliedLabel(true, outcome, &clip, "cinematic"));
    outcome.begin();
    ASSERT_EQ(std::string("Look: Cinematic"), appliedLabel(true, outcome, &clip, "cinematic"));

    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString beginCall = QStringLiteral("m_lookAssistFlavorOutcome.begin();");
    const int begin = mw.indexOf(beginCall);
    ASSERT_TRUE(begin >= 0);
    ASSERT_EQ(-1, mw.indexOf(beginCall, begin + 1));   // one analysis entry point
    ASSERT_TRUE(mw.indexOf(QStringLiteral("// No updateLookLabel() here on purpose"), begin) > begin);
    const int firstLanded = mw.indexOf(QStringLiteral("m_lookAssistFlavorOutcome.landed("), begin);
    ASSERT_TRUE(firstLanded > begin);
    ASSERT_FALSE(mw.mid(begin, firstLanded - begin).contains(QStringLiteral("updateLookLabel();")));
}

// CoS r6 (PR #459): the pin above covers only the text from begin() to the first landed(). While the analysis is
// pending other callers re-derive the label -- a fullscreen toggle (on_actionFullscreen_triggered) and setSliders --
// and after a fallback they named the selected flavor while the baseline was still on screen. The pending state now
// lives in the ONE derivation, look_label::shownText, which every caller reaches through updateLookLabel.
TEST(LookLabelPolicy, PendingAnalysisPinHoldsForEveryRederivation)
{
    int clip = 0, otherClip = 0;
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    const auto shown = [&](bool on, const void *receipt) {
        return look_label::shownText(on, outcome, receipt, cinematic).toStdString();
    };
    const std::string baseline("Look: Baseline (fallback)");

    outcome.landed(&clip, cinematic, true);   // the analysis fell back: the baseline sliders are on screen
    ASSERT_EQ(baseline, shown(true, &clip));
    outcome.begin();                          // a new analysis is pending; the baseline is still on screen
    ASSERT_EQ(baseline, shown(true, &clip));  // fullscreen toggle mid-analysis: updateLookLabel re-derives
    ASSERT_EQ(baseline, shown(true, &clip));  // setSliders mid-analysis: updateLookLabel re-derives
    outcome.begin();                          // the sync pass re-running as master's is still the same pending run
    ASSERT_EQ(baseline, shown(true, &clip));
    // Only the label is pinned: the receipt and the telemetry still read the selector's value while pending.
    ASSERT_EQ(std::string("cinematic"), outcome.receiptValue(&clip, cinematic).toStdString());
    ASSERT_FALSE(outcome.fellBack(&clip));
    ASSERT_TRUE(outcome.appliedName().isEmpty());
    // Look Assist off still wins, and another clip's label is not this clip's fallback (nothing landed on it: r9 A1,
    // the baseline, never the selected flavor).
    ASSERT_EQ(std::string("Look: Off"), shown(false, &clip));
    ASSERT_EQ(std::string("Look: Baseline"), shown(true, &otherClip));
    // It lands applied: the flavor applied.
    outcome.landed(&clip, cinematic, false);
    ASSERT_EQ(std::string("Look: Cinematic"), shown(true, &clip));
    // Pending after an applied grade keeps naming it, and a fallback landing then names the baseline.
    outcome.begin();
    ASSERT_EQ(std::string("Look: Cinematic"), shown(true, &clip));
    outcome.landed(&clip, cinematic, true);
    ASSERT_EQ(baseline, shown(true, &clip));
    // clear() (Look Assist off, flavor changed) ends the pending run; the baseline is restored, so the label says so
    // (r9 A2: it used to name the selector's flavor before the new analysis landed).
    outcome.begin();
    outcome.clear();
    ASSERT_EQ(std::string("Look: Baseline"), shown(true, &clip));
}

// r9 adversary A1/A2 (e3704ee7): the label names a flavor only once an analysis LANDED on this receipt.
// A1: Look Assist on with the analysis never running (MLVAPP_NO_LOOK_ASSIST, or an apply.skip) -- nothing ever lands, the
//     baseline stays on screen, and the label must not read "Look: Cinematic" forever.
// A2: a landed fallback, then the flavor changes: the dropdown clears the outcome and restores the baseline, and the new
//     analysis begin()s from that empty outcome -- every re-derivation until it lands shows the baseline, not "Film grade".
TEST(LookLabelPolicy, R9AFlavorIsNamedOnlyOnceAnAnalysisLandedOnThisReceipt)
{
    int clip = 0;
    const QString cinematic = QStringLiteral("cinematic");
    const QString film = QStringLiteral("film");
    const auto shown = [&](const lookassist::LookAssistFlavorOutcome &o, const QString &selected) {
        return look_label::shownText(true, o, &clip, selected).toStdString();
    };

    lookassist::LookAssistFlavorOutcome never;                    // A1: no analysis ever ran
    ASSERT_EQ(std::string("Look: Baseline"), shown(never, cinematic));
    // The receipt and the telemetry keep the selector's value: only the label changed.
    ASSERT_EQ(std::string("cinematic"), never.receiptValue(&clip, cinematic).toStdString());

    lookassist::LookAssistFlavorOutcome outcome;                  // A2
    outcome.landed(&clip, cinematic, true);
    ASSERT_EQ(std::string("Look: Baseline (fallback)"), shown(outcome, cinematic));
    outcome.clear();                                              // on_comboBoxLookAssistFlavor_currentIndexChanged
    ASSERT_EQ(std::string("Look: Baseline"), shown(outcome, film));
    outcome.begin();                                              // the replacement analysis is pending
    ASSERT_EQ(std::string("Look: Baseline"), shown(outcome, film));
    ASSERT_TRUE(shown(outcome, film).find("Film") == std::string::npos);
    outcome.landed(&clip, film, false);                           // it lands applied
    ASSERT_EQ(std::string("Look: Film grade"), shown(outcome, film));
    // Off still wins.
    ASSERT_EQ(std::string("Look: Off"), look_label::shownText(false, outcome, &clip, film).toStdString());
}

// Adversary r11 A1. Clip A is loaded with a landed Cinematic grade and Look Assist on; an empty clip B is imported, so
// addFileToSession makes B the active row and pumps events before B's loader runs. B's receipt defaults Look Assist on and
// nothing landed on it. F11 in that pump re-derives the label, and A is still what is on screen. No clip is opened here.
TEST(LookLabelPolicy, A1LabelNamesTheReceiptThatOwnsTheLoadedClipNotTheActiveRow)
{
    int receiptA = 0, receiptB = 0;
    const void *receipts[] = { &receiptA, &receiptB };
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    outcome.landed(&receiptA, cinematic, false);

    const int rowA = 0, rowB = 1, rows = 2;
    int loadedRow = rowA;
    const auto isLoadedRow = [&loadedRow](int row) { return row == loadedRow; };
    // The label as updateLookLabel derives it, with the checkbox still describing A.
    const auto label = [&](int activeRow) {
        const int owner = look_label::loadedClipOwnerRow(activeRow, rows, isLoadedRow);
        const bool on = look_label::lookOnForOwner(owner >= 0, true, owner >= 0 && owner == activeRow, true);
        return look_label::shownText(on, outcome, on ? receipts[owner] : nullptr, cinematic).toStdString();
    };

    // The pairing A1 reported: A's outcome read through B's receipt would say "Look: Baseline" over Cinematic A.
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, outcome, &receiptB, cinematic).toStdString());
    // B is active and A is loaded: the owner is A, and the label keeps naming A's look.
    ASSERT_EQ(rowA, look_label::loadedClipOwnerRow(rowB, rows, isLoadedRow));
    ASSERT_EQ(std::string("Look: Cinematic"), label(rowB));
    // B's open succeeds: B owns the clip, and nothing has landed on it.
    loadedRow = rowB;
    ASSERT_EQ(rowB, look_label::loadedClipOwnerRow(rowB, rows, isLoadedRow));
    ASSERT_EQ(std::string("Look: Baseline"), label(rowB));
}

// Adversary r12 A1. Clip A is loaded with Cinematic landed; Open Session's Search gives a second row A's path (that branch has
// no duplicate check), so the row is active before its loader runs, and the name alone matches both rows. F11 in that pump.
TEST(LookLabelPolicy, A1r12SamePathTwiceTheRecordedRowOwnsTheClip)
{
    int receiptA = 0, receiptB = 0;
    const void *receipts[] = { &receiptA, &receiptB };
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    outcome.landed(&receiptA, cinematic, false);

    const int rowA = 0, rowB = 1, rows = 2;
    const void *recorded = &receiptA;
    const auto holdsPath = [](int) { return true; };   // both rows hold the loaded path
    const auto isRecorded = [&](int row) { return receipts[row] == recorded; };
    const auto isLoadedRow = [&](int row) {
        return holdsPath(row) && !loadedClipRecordedOnAnotherRow(row, rows, holdsPath, isRecorded);
    };

    // The row's own open has not run: the recorded row owns the clip, and the label keeps naming A's look.
    ASSERT_FALSE(isLoadedRow(rowB));
    ASSERT_TRUE(isLoadedRow(rowA));
    const int before = look_label::loadedClipOwnerRow(rowB, rows, isLoadedRow);
    ASSERT_EQ(rowA, before);
    ASSERT_EQ(std::string("Look: Cinematic"), look_label::shownText(true, outcome, receipts[before], cinematic).toStdString());
    // Its open succeeds and records it: the new row owns the clip, and nothing has landed on it.
    recorded = &receiptB;
    const int after = look_label::loadedClipOwnerRow(rowB, rows, isLoadedRow);
    ASSERT_EQ(rowB, after);
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, outcome, receipts[after], cinematic).toStdString());
    // Nothing recorded in the session (the recorded row is gone): the name decides and the active row breaks the tie.
    recorded = nullptr;
    ASSERT_EQ(rowB, look_label::loadedClipOwnerRow(rowB, rows, isLoadedRow));
    // A recorded row that does not hold the path never takes a clip from the row that does.
    recorded = &receiptA;
    const auto onlyB = [](int row) { return row == 1; };
    ASSERT_FALSE(loadedClipRecordedOnAnotherRow(rowB, rows, onlyB, isRecorded));
}

TEST(LookLabelWiring, A1r12EveryOwnershipPredicateHonoursTheRecordedRow)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(functionBody(mw, QStringLiteral("bool MainWindow::loadedClipIsRow( int row ) const"))
                    .contains(QStringLiteral("&& !loadedClipRecordedOnAnotherRow( row );")));
    ASSERT_TRUE(functionBody(mw, QStringLiteral("bool MainWindow::receiptOwnedByLoadedClip( const char *site )"))
                    .contains(QStringLiteral("&& !loadedClipRecordedOnAnotherRow( SESSION_ACTIVE_CLIP_ROW );")));
    // Both opens record it in the step that names the file.
    const QString recordThenName = QStringLiteral("recordLoadedClipReceipt( fileName );\n    m_loadedClipFileName = fileName;");
    ASSERT_EQ(2, mw.count(recordThenName));
    ASSERT_EQ(2, mw.count(QStringLiteral("m_loadedClipFileName = fileName;")));
    ASSERT_EQ(1, mw.count(QStringLiteral("m_loadedClipReceipt = ")));
}

// Adversary r12 A2: a windowed clip with the GPU device-loss notice up. That notice is showMessage( text, 0 ), and QStatusBar
// hides a normal widget during a message, so the look would be off the GUI until the notice clears. Pinned as source
// because console_tests links no widgets; the pixels are owed to the venue leg.
TEST(LookLabelWiring, A2r12WindowedLabelIsAPermanentWidgetSoNoMessageHidesIt)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mw.contains(QStringLiteral("statusBar()->addPermanentWidget( m_pLookLabelStatus );")));
    ASSERT_FALSE(mw.contains(QStringLiteral("statusBar()->addWidget( m_pLookLabelStatus );")));
}

TEST(LookLabelPolicy, A1OwnerRowEdgeCases)
{
    const auto never = [](int) { return false; };
    const auto always = [](int) { return true; };
    ASSERT_EQ(-1, look_label::loadedClipOwnerRow(0, 2, never));   // no row holds the clip
    ASSERT_EQ(-1, look_label::loadedClipOwnerRow(0, 0, always));  // an empty session
    ASSERT_EQ(1, look_label::loadedClipOwnerRow(1, 3, always));   // the same path twice: the active row wins
    ASSERT_EQ(0, look_label::loadedClipOwnerRow(-1, 3, always));  // no valid active row: the scan still finds it
    ASSERT_EQ(0, look_label::loadedClipOwnerRow(9, 3, always));   // a stale active row is never read
}

TEST(LookLabelPolicy, A1SelectorCountsOnlyWhenTheOwnerIsTheActiveRow)
{
    // The owner is the active row: the switch and the checkbox, as before.
    ASSERT_TRUE(look_label::lookOnForOwner(true, true, true, true));
    ASSERT_FALSE(look_label::lookOnForOwner(true, true, true, false));
    ASSERT_FALSE(look_label::lookOnForOwner(true, false, true, true));
    // B is active: unticking the box writes nothing into A (receiptOwnedByLoadedClip refuses it), so A's look stays named.
    ASSERT_TRUE(look_label::lookOnForOwner(true, true, false, false));
    ASSERT_FALSE(look_label::lookOnForOwner(true, false, false, true));
    // No owner: never on.
    ASSERT_FALSE(look_label::lookOnForOwner(false, true, false, true));
}

TEST(LookLabelPolicy, A1NoOwnerKeepsTheTextOnlyWhileAClipIsLoaded)
{
    ASSERT_TRUE(look_label::keepsTextWithoutOwner(true, -1));     // loaded, in no row: no receipt can say otherwise
    ASSERT_FALSE(look_label::keepsTextWithoutOwner(true, 0));     // an owner derives the text
    ASSERT_FALSE(look_label::keepsTextWithoutOwner(false, -1));   // nothing loaded: "Look: Off"
}

TEST(LookLabelWiring, A1UpdateLookLabelDerivesFromTheOwnerNeverTheActiveReceipt)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_FALSE(update.contains(QStringLiteral("ACTIVE_RECEIPT")));
    ASSERT_TRUE(update.contains(QStringLiteral("look_label::loadedClipOwnerRow(")));
    ASSERT_TRUE(update.contains(QStringLiteral("loadedClipIsRow( row )")));
    // The whole call, so an inverted argument fails here (tools/.../policy_callsite_mutation_smoke.py).
    ASSERT_TRUE(update.contains(QStringLiteral(
        "look_label::lookOnForOwner( owner != nullptr, owner && owner->lookAssistEnabled(),\n"
        "                                                    owner && ownerRow == SESSION_ACTIVE_CLIP_ROW,\n"
        "                                                    ui->checkBoxLookAssistEnable->isChecked() );")));
    ASSERT_TRUE(update.contains(QStringLiteral("const void *lookReceipt = lookOn ? owner : nullptr;")));
    ASSERT_TRUE(update.contains(QStringLiteral("look_label::keepsTextWithoutOwner( m_fileLoaded, ownerRow ) ? m_lookLabelText")));
    // Called from settings loading before the session model exists: the model is read only once a clip is loaded.
    ASSERT_TRUE(update.contains(QStringLiteral("const int ownerRow = !m_fileLoaded ? -1 : look_label::loadedClipOwnerRow(")));
    // The pump A1 used still sits between the activation and the loader.
    const QString add = functionBody(mw, QStringLiteral("ClipInformation *MainWindow::addFileToSession(QString fileName)"));
    const int activated = add.indexOf(QStringLiteral("m_pModel->setActiveRow( SESSION_CLIP_COUNT - 1 );"));
    ASSERT_TRUE(activated >= 0 && add.indexOf(QStringLiteral("qApp->processEvents();"), activated) > activated);
}

TEST(LookLabelWiring, EveryLabelDerivationGoesThroughTheOnePendingAwareHelper)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // One derivation in the whole window, in updateLookLabel; no caller builds its own label text.
    const QString helperCall = QStringLiteral("look_label::shownText(");
    const int at = mw.indexOf(helperCall);
    ASSERT_TRUE(at >= 0);
    ASSERT_EQ(-1, mw.indexOf(helperCall, at + 1));
    ASSERT_TRUE(functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )")).contains(helperCall));
    ASSERT_FALSE(mw.contains(QStringLiteral("look_label::appliedText(")));
    // The two re-deriving callers the CoS named reach the label only through updateLookLabel.
    const char *callers[] = {
        "void MainWindow::on_actionFullscreen_triggered( bool checked )",
        "void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)",
    };
    for ( const char *caller : callers )
    {
        const QString body = functionBody(mw, QString::fromLatin1(caller));
        ASSERT_TRUE(body.contains(QStringLiteral("updateLookLabel();")));
        ASSERT_FALSE(body.contains(QStringLiteral("look_label::")));
    }
    // The helper reads the outcome ON SCREEN (the last landed one while pending), not the reported one.
    const QString policy = readRepoFile(QStringLiteral("platform/qt/LookLabelPolicy.h"));
    ASSERT_TRUE(functionBody(policy, QStringLiteral("inline QString shownText("))
                    .contains(QStringLiteral("outcome.shown()")));
}

// The documented gap (PR #459 Known gap): MLVAPP_EXPERIMENTAL_GL_VIEWPORT installs GpuDisplayViewport, which owns the
// viewport's Paint while a GPU frame is pending, so GraphicsZoomView::drawForeground (the CPU corner label) is never
// reached and nothing else draws the label there. Pinned so a change to that route fails here, not silently on screen.
TEST(LookLabelWiring, GlViewportRouteIsExcludedFromTheCornerLabel)
{
    const QString vp = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    // The route: only the environment variable installs it, and only when the GL window is not the preview.
    ASSERT_TRUE(functionBody(vp, QStringLiteral("const char *GpuDisplayViewport::environmentVariableName()"))
                    .contains(QStringLiteral("return \"MLVAPP_EXPERIMENTAL_GL_VIEWPORT\";")));
    ASSERT_TRUE(functionBody(vp, QStringLiteral("bool GpuDisplayViewport::installOn(QGraphicsView *view)"))
                    .contains(QStringLiteral("if ( !view || !isRequestedByEnvironment() ) return false;")));
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mw.contains(QStringLiteral("    if ( !glWindowPreview )\n        GpuDisplayViewport::installOn( ui->graphicsView );")));
    // It swallows Paint while a frame is pending: QGraphicsView's paint, and so drawForeground, does not run.
    const QString filter = functionBody(vp, QStringLiteral("bool GpuDisplayViewport::eventFilter(QObject *watched, QEvent *event)"));
    ASSERT_TRUE(filter.contains(QStringLiteral("event->type() == QEvent::Paint && hasPendingFrame()")));
    ASSERT_TRUE(filter.contains(QStringLiteral("QOpenGLWidget::event(event);\n        return true;")));
    // And it draws no label of its own; the corner label lives only in GraphicsZoomView and GpuDisplayWindow.
    ASSERT_FALSE(vp.contains(QStringLiteral("looklabel"), Qt::CaseInsensitive));
    ASSERT_FALSE(readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.h"))
                     .contains(QStringLiteral("looklabel"), Qt::CaseInsensitive));
    // updateLookLabel has no GL-viewport branch: the viewport gets the text and the status bar still shows it.
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_FALSE(update.contains(QStringLiteral("GpuDisplayViewport")));
    ASSERT_TRUE(update.contains(QStringLiteral("m_pLookLabelStatus->setText( text )")));
}

TEST(LookLabelPolicy, CornerLabelOnlyInFullscreenAndOnlyWhenShown)
{
    ASSERT_TRUE(look_label::fullscreenOverlayShown(true, true));
    ASSERT_FALSE(look_label::fullscreenOverlayShown(true, false));
    ASSERT_FALSE(look_label::fullscreenOverlayShown(false, true));
    ASSERT_FALSE(look_label::fullscreenOverlayShown(false, false));
}

TEST(LookLabelPolicy, PlacedInTheBottomRightCornerAndSmall)
{
    const QSize uhd(3840, 2160);
    const int fontPx = look_label::fontPixels(uhd.height());
    ASSERT_EQ(40, fontPx);
    ASSERT_EQ(20, look_label::fontPixels(1080));
    ASSERT_EQ(14, look_label::fontPixels(200));    // never unreadably small
    ASSERT_EQ(64, look_label::fontPixels(8000));   // never large
    const int margin = look_label::marginPixels(fontPx);
    const QRect r = look_label::overlayRect(uhd, QSize(300, 60), margin);
    ASSERT_EQ(uhd.width() - margin, r.right() + 1);
    ASSERT_EQ(uhd.height() - margin, r.bottom() + 1);
    ASSERT_EQ(300, r.width());
    ASSERT_EQ(60, r.height());
    // Away from the centre: wholly in the bottom-right quadrant.
    ASSERT_TRUE(r.left() > uhd.width() / 2 && r.top() > uhd.height() / 2);
}

TEST(LookLabelPolicy, PlacementNeverLeavesTheSurface)
{
    const QRect r = look_label::overlayRect(QSize(100, 40), QSize(300, 60), 16);
    ASSERT_TRUE(QRect(0, 0, 100, 40).contains(r));
    ASSERT_TRUE(look_label::overlayRect(QSize(), QSize(10, 10), 4).isEmpty());
    ASSERT_TRUE(look_label::overlayRect(QSize(100, 100), QSize(), 4).isEmpty());
}

TEST(LookLabelPolicy, HideSettingDefaultsToShownAndPersists)
{
    ASSERT_EQ(std::string("Playback/ShowLookLabelInFullscreen"),
              std::string(look_label::Settings::kKeyShowInFullscreen()));
    ASSERT_TRUE(look_label::Settings::kDefaultShowInFullscreen());

    std::unique_ptr<QSettings> store = automation_settings::openAppSettings();
    const QVariant saved = store->value(look_label::Settings::kKeyShowInFullscreen());
    store->remove(look_label::Settings::kKeyShowInFullscreen());
    store->sync();

    ASSERT_TRUE(look_label::showInFullscreenFromSettings());
    look_label::showInFullscreenWriteToSettings(false);
    ASSERT_FALSE(automation_settings::openAppSettings()->value(
        look_label::Settings::kKeyShowInFullscreen(), true).toBool());
    ASSERT_FALSE(look_label::showInFullscreenFromSettings());
    look_label::showInFullscreenWriteToSettings(true);
    ASSERT_TRUE(look_label::showInFullscreenFromSettings());

    if ( saved.isValid() ) store->setValue(look_label::Settings::kKeyShowInFullscreen(), saved);
    else store->remove(look_label::Settings::kKeyShowInFullscreen());
    store->sync();
}

TEST(LookLabelWiring, LabelReadsTheGradesStateAndUpdatesOnEveryChange)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    // One source of truth: the switch of the receipt that owns the loaded clip, the checkbox and the flavor the grade uses.
    ASSERT_TRUE(update.contains(QStringLiteral("owner->lookAssistEnabled()")));
    ASSERT_TRUE(update.contains(QStringLiteral("ui->checkBoxLookAssistEnable->isChecked()")));
    ASSERT_TRUE(update.contains(QStringLiteral("lookAssistFlavorName( currentLookAssistFlavor() )")));
    // Once per change: nothing is pushed when neither the text nor the fullscreen state changed.
    ASSERT_TRUE(update.contains(QStringLiteral(
        "if( text == m_lookLabelText && fullscreenShown == m_lookLabelFullscreenShown ) return;")));
    ASSERT_TRUE(update.contains(QStringLiteral("GpuDisplayWindow::setLookLabelIfActive( overlayText )")));
    ASSERT_TRUE(update.contains(QStringLiteral("ui->graphicsView->setLookLabel(")));
    ASSERT_TRUE(update.contains(QStringLiteral("m_pLookLabelStatus->setText( text )")));

    // Every change path re-derives it: flavor, the switch (click and programmatic), fullscreen, the setting,
    // a receipt's values, and the smoke's forced-off look.
    const char *callers[] = {
        "void MainWindow::on_comboBoxLookAssistFlavor_currentIndexChanged( int )",
        "void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )",
        "void MainWindow::on_actionFullscreen_triggered( bool checked )",
        "void MainWindow::on_actionShowLookLabelInFullscreen_triggered()",
    };
    for ( const char *caller : callers )
    {
        ASSERT_TRUE(functionBody(mw, QString::fromLatin1(caller)).contains(QStringLiteral("updateLookLabel();")));
    }
    ASSERT_TRUE(mw.contains(QStringLiteral(
        "connect( ui->checkBoxLookAssistEnable, &QCheckBox::toggled, this, [this]( bool ) { updateLookLabel(); } );")));
    ASSERT_TRUE(mw.contains(QStringLiteral("updateLookLabel();   // both signals were blocked above")));
    // The setting is the persisted one, read where the other playback settings are read.
    ASSERT_TRUE(mw.contains(QStringLiteral("m_lookLabelShowInFullscreen = look_label::showInFullscreenFromSettings();")));
    ASSERT_TRUE(mw.contains(QStringLiteral("look_label::showInFullscreenWriteToSettings( m_lookLabelShowInFullscreen );")));
}

TEST(LookLabelWiring, ReceiptLineReportsTheTextTheSurfaceDrew)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mw.contains(QStringLiteral("gui_smoke.look_label shown=\\\"%1\\\" expected=\\\"%2\\\" surface=%3")));
    ASSERT_TRUE(mw.contains(QStringLiteral("shown = GpuDisplayWindow::lookLabelDrawnTextIfActive();")));
    ASSERT_TRUE(mw.contains(QStringLiteral("shown = ui->graphicsView->lookLabelDrawnText();")));

    // The GPU window draws the label after the frame and before the capture readback (captures show it), and
    // records what it drew; the CPU viewport blits its cached pixmap in drawForeground.
    const QString win = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int drawAt = win.indexOf(QStringLiteral("    drawLookLabel(fbw, fbh);"));
    const int readbackAt = win.indexOf(QStringLiteral("    if ( m_captureReadbackRequested )\n    {\n        if ( fbw <= 0"));
    ASSERT_TRUE(drawAt >= 0 && readbackAt > drawAt);
    ASSERT_TRUE(win.contains(QStringLiteral("m_lookLabelDrawnText = m_lookLabelText;")));
    const QString view = readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.cpp"));
    ASSERT_TRUE(view.contains(QStringLiteral("void GraphicsZoomView::drawForeground(QPainter *painter, const QRectF &rect)")));
    ASSERT_TRUE(view.contains(QStringLiteral("m_lookLabelDrawnText = m_lookLabelText;")));
}

TEST(LookLabelWiring, TheLabelNeverTouchesTheGrade)
{
    // The grade's inputs are untouched by the label: updateLookLabel never writes the receipt or the Look Assist UI.
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_FALSE(update.contains(QStringLiteral("->setLookAssist")));
    ASSERT_FALSE(update.contains(QStringLiteral("->setChecked(")));
    ASSERT_FALSE(update.contains(QStringLiteral("setCurrentIndex(")));
    ASSERT_FALSE(update.contains(QStringLiteral("previewProcessing")));
}

// r11 (adversary r10 A1 on 0b840efd): a paste onto the active row (or any setSliders of the active receipt) puts the
// baseline back, so the label reads "Look: Baseline", not the flavor that landed before -- until an analysis lands.
// r10 (sol + fable + key3 r9 on 2b748fa3): the mark used to be a MainWindow member (m_lookLabelBaselineReceipt) set only
// when the de-dupe marker named the receipt and cleared only at begin(), so it outlived the event that made it true. It
// is now the outcome's own (LookAssistFlavorOutcome::baselineRestored), keyed by the outcome's receipt identity, and
// landed() forgets it in the same step it records the outcome -- one place for all five landing sites.
TEST(LookLabelWiring, ABaselineRestoredWithoutARegradeIsLabelledBaseline)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(mw, QStringLiteral("void MainWindow::setSliders(ReceiptSettings *receipt, bool paste)"));
    ASSERT_FALSE(body.isEmpty());
    const int restore = body.indexOf(QStringLiteral("restoreLookAssistBaseline( receipt );"));
    // r13 (adversary r12 A1 on 6d722d33): the fresh-capture branch (Reset Receipt: baseline invalid) is marked the same way.
    const int capture = body.indexOf(QStringLiteral("captureLookAssistBaseline( receipt );"));
    const int markRead = body.indexOf(QStringLiteral("baselineRestoredUngraded = receipt == activeReceiptAtLoad;"));
    const int marked = body.indexOf(QStringLiteral("if( baselineRestoredUngraded ) m_lookAssistFlavorOutcome.baselineRestored( receipt );"));
    const int apply = body.indexOf(QStringLiteral("&& !m_lookAssistApplied.isApplied( receipt ) )"));
    const int relabelAt = body.indexOf(QStringLiteral("if( baselineRestoredUngraded )\n"));
    const int relabel = body.indexOf(QStringLiteral("updateLookLabel();"), relabelAt);
    printf("[LL10] setSliders: restore=%d capture=%d markRead=%d marked=%d apply=%d relabelAt=%d relabel=%d\n",
           restore, capture, markRead, marked, apply, relabelAt, relabel);
    ASSERT_TRUE(restore >= 0);
    ASSERT_TRUE(capture > restore);
    ASSERT_TRUE(markRead > capture);    // after both branches: restored OR captured
    ASSERT_TRUE(marked > markRead);
    ASSERT_TRUE(apply > marked);        // marked BEFORE the apply block, so a grade applied there lands and is named
    ASSERT_TRUE(relabelAt > apply);
    ASSERT_TRUE(relabel > relabelAt);
    ASSERT_EQ(1, body.count(QStringLiteral("m_lookAssistFlavorOutcome.baselineRestored(")));
    // key3 r9 B1: the mark never reads the de-dupe marker (it may name another clip whose analysis skipped before begin()).
    ASSERT_FALSE(body.contains(QStringLiteral("baselineRestoredUngraded = m_lookAssistApplied.isApplied(")));
    // r12 (adversary r11 A1 on 4337dad4): LABEL ONLY -- setSliders never alters the reported outcome, which setReceipt
    // saves as the receipt's flavor ("classic" after a fallback must stay "classic").
    ASSERT_FALSE(body.contains(QStringLiteral("m_lookAssistFlavorOutcome.clear();")));
    // Pin count update (r11, key3 r10 L-07; was 1): setSliders and the Look Assist toggle-ON restore/capture
    // (LookLabelWiring.L07ToggleOnRestoreIsLabelledBaseline pins the second).
    ASSERT_EQ(2, mw.count(QStringLiteral("m_lookAssistFlavorOutcome.baselineRestored(")));
    // Pin count update (was ASSERT_EQ(2, count("m_lookLabelBaselineReceipt = "))): the member and both of its writes (the
    // setSliders mark and the begin() reset) are gone; the mark lives in the outcome, so no landing site can forget it.
    ASSERT_EQ(0, mw.count(QStringLiteral("m_lookLabelBaselineReceipt")));
    ASSERT_EQ(0, readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).count(QStringLiteral("m_lookLabelBaselineReceipt")));
    // updateLookLabel passes the receipt that owns the loaded clip through (r12, adversary r11 A1); shownText reads the mark.
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_TRUE(update.contains(QStringLiteral("const void *lookReceipt = lookOn ? owner : nullptr;")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/LookLabelPolicy.h"));
    ASSERT_TRUE(functionBody(policy, QStringLiteral("inline QString shownText("))
                    .contains(QStringLiteral("if( outcome.showsBaseline( receipt ) ) return QStringLiteral( \"Look: Baseline\" );")));
    // The policy: the mark reads Baseline for its receipt only, and the receipt value (what setReceipt saves) is unchanged.
    lookassist::LookAssistFlavorOutcome outcome;
    int receipt = 0, other = 0;
    const QString cinematic = QStringLiteral("cinematic");
    outcome.landed(&receipt, cinematic, true);
    outcome.baselineRestored(&receipt);
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, outcome, &receipt, cinematic).toStdString());
    ASSERT_EQ(std::string("classic"), outcome.receiptValue(&receipt, cinematic).toStdString());
    ASSERT_TRUE(outcome.fellBack(&receipt));
    outcome.landed(&receipt, cinematic, false);
    outcome.baselineRestored(&receipt);
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, outcome, &receipt, cinematic).toStdString());
    ASSERT_EQ(std::string("cinematic"), outcome.receiptValue(&receipt, cinematic).toStdString());
    ASSERT_FALSE(outcome.showsBaseline(&other));
    ASSERT_FALSE(outcome.showsBaseline(nullptr));
    ASSERT_EQ(std::string("Look: Off"), look_label::shownText(false, outcome, &receipt, cinematic).toStdString());
}

// r10 (a) -- catalogue L-05, sol + fable r9 on 2b748fa3. MLVAPP_LOOK_ASSIST_ASYNC=1: the analysis on R is dispatched
// (begin()), a paste or Reset Receipt onto R restores its baseline (setSliders marks R), then the queued result lands
// applied. RED on 2b748fa3: the mark was cleared only at begin(), which ran BEFORE the paste, so the landing kept
// "Look: Baseline" over the applied Cinematic grade.
TEST(LookLabelPolicy, R10AMarkThenAsyncLandingNamesTheLandedFlavor)
{
    int r = 0;
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    const auto shown = [&]() { return look_label::shownText(true, outcome, &r, cinematic).toStdString(); };
    outcome.begin();                 // async dispatch on R (applyLookAssistToReceipt -> begin(); worker detached)
    ASSERT_EQ(std::string("Look: Baseline"), shown());
    outcome.baselineRestored(&r);    // paste / Reset Receipt onto R while the worker is in flight
    ASSERT_EQ(std::string("Look: Baseline"), shown());
    outcome.landed(&r, cinematic, false);   // the queued result applies Cinematic and lands (MainWindow.cpp async applied)
    ASSERT_EQ(std::string("Look: Cinematic"), shown());
    ASSERT_FALSE(outcome.showsBaseline(&r));
    // The sync route the same: the mark, then a landing in the same call.
    lookassist::LookAssistFlavorOutcome sync;
    sync.landed(&r, cinematic, false);
    sync.baselineRestored(&r);
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, sync, &r, cinematic).toStdString());
    sync.begin();                    // a re-grade starts: the baseline is still on screen until it lands (key3 H1)
    ASSERT_EQ(std::string("Look: Baseline"), look_label::shownText(true, sync, &r, cinematic).toStdString());
    sync.landed(&r, cinematic, false);
    ASSERT_EQ(std::string("Look: Cinematic"), look_label::shownText(true, sync, &r, cinematic).toStdString());
}

// r10 (b) -- catalogue L-05 sibling: the async safety-fallback landings (fail-closed and post-measure) after the same
// paste keep their suffix. RED on 2b748fa3: the surviving mark read the fallback as "Look: Baseline".
TEST(LookLabelPolicy, R10BFallbackLandingAfterAMarkKeepsItsSuffix)
{
    int r = 0;
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    outcome.begin();
    outcome.baselineRestored(&r);
    outcome.landed(&r, cinematic, true);
    ASSERT_EQ(std::string("Look: Baseline (fallback)"), look_label::shownText(true, outcome, &r, cinematic).toStdString());
    ASSERT_EQ(std::string("classic"), outcome.receiptValue(&r, cinematic).toStdString());
    // clear() (Look Assist off / flavor change) forgets the mark too.
    outcome.baselineRestored(&r);
    outcome.clear();
    ASSERT_FALSE(outcome.showsBaseline(&r));
}

// r10 (c) -- catalogue L-06, key3 r9 B1 on 2b748fa3 (shipped default SYNC route). A lands Cinematic; Y's apply skips
// before begin() (empty_stats: black / lens-cap clip) but Y is marked applied, so the de-dupe marker moves to Y while
// the outcome stays on A. setSliders(A) restores A's baseline. RED on 2b748fa3: the mark was set only when
// isApplied(A) -- false here -- so the label read "Look: Cinematic" over A's ungraded baseline.
TEST(LookLabelPolicy, R10CSkippedAnalysisOnAnotherClipNeverLeavesAFlavorOverARestoredBaseline)
{
    int a = 0, y = 0;
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    lookassist::LookAssistAppliedMarker applied;
    const auto shownFor = [&](const void *receipt) { return look_label::shownText(true, outcome, receipt, cinematic).toStdString(); };

    outcome.landed(&a, cinematic, false);   // open A: its frame-ready analysis lands
    applied.markApplied(&a);
    ASSERT_EQ(std::string("Look: Cinematic"), shownFor(&a));
    outcome.baselineRestored(&y);           // select Y: setSliders(Y) restores Y's baseline
    applied.markApplied(&y);                // Y's apply skipped before begin() (no outcome change), Y still marked applied
    ASSERT_EQ(std::string("Look: Baseline"), shownFor(&y));
    // Back to A: the de-dupe marker no longer names A -- the old rule (mark iff isApplied) would not mark, and the label
    // would read the stale landing:
    ASSERT_FALSE(applied.isApplied(&a));
    ASSERT_EQ(std::string("Look: Cinematic"), shownFor(&a));   // what 2b748fa3 showed over the restored baseline
    outcome.baselineRestored(&a);           // the r10 rule: setSliders(A) restores A's baseline -> marked, whatever the marker says
    ASSERT_EQ(std::string("Look: Baseline"), shownFor(&a));
    ASSERT_TRUE(shownFor(&a).find("Cinematic") == std::string::npos);
    // A's deferred re-grade (not deduped: the marker names Y) lands: named again.
    outcome.begin();
    ASSERT_EQ(std::string("Look: Baseline"), shownFor(&a));
    outcome.landed(&a, cinematic, false);
    ASSERT_EQ(std::string("Look: Cinematic"), shownFor(&a));
}

// r10: every landing goes through LookAssistFlavorOutcome::landed, which forgets the mark in the same step -- pinned in
// the header so a landing cannot record an outcome and leave the mark (the r9 class).
TEST(LookLabelWiring, R10EveryLandingForgetsTheBaselineMarkInTheSameStep)
{
    const QString fullHeader = readRepoFile(QStringLiteral("src/batch/LookAssistAnalysis.h"));
    // Only the outcome class (LookAssistAppliedMarker above it has its own clear()).
    const int classAt = fullHeader.indexOf(QStringLiteral("class LookAssistFlavorOutcome\n"));
    ASSERT_TRUE(classAt >= 0);
    const QString header = fullHeader.mid(classAt, fullHeader.indexOf(QStringLiteral("\n};"), classAt) - classAt);
    const QString landed = functionBody(header, QStringLiteral("    void landed( const void *receipt, const QString &flavorName, bool safetyFallback )"));
    // functionBody stops at a column-0 brace; the method's own closing brace is indented, so bound it explicitly.
    const QString landedBody = landed.left(landed.indexOf(QStringLiteral("\n    }")));
    ASSERT_TRUE(landedBody.contains(QStringLiteral("m_receipt = receipt;")));
    ASSERT_TRUE(landedBody.contains(QStringLiteral("m_baselineShownReceipt = nullptr;")));
    const int clearAt = header.indexOf(QStringLiteral("    void clear() {"));
    ASSERT_TRUE(clearAt >= 0);
    ASSERT_TRUE(header.mid(clearAt, header.indexOf(QLatin1Char('\n'), clearAt) - clearAt)
                    .contains(QStringLiteral("m_baselineShownReceipt = nullptr;")));
    const int beginAt = header.indexOf(QStringLiteral("    void begin()"));
    ASSERT_TRUE(beginAt >= 0);
    const QString beginBody = header.mid(beginAt, header.indexOf(QStringLiteral("\n    }"), beginAt) - beginAt);
    ASSERT_FALSE(beginBody.contains(QStringLiteral("m_baselineShownReceipt")));   // begin() keeps it: baseline still on screen
    // Five landing sites in the window, each through landed() (L04 pin counts them); none writes the mark itself.
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(5, mw.count(QStringLiteral("m_lookAssistFlavorOutcome.landed(")));
    ASSERT_EQ(0, mw.count(QStringLiteral("m_baselineShownReceipt")));
}

// r11 -- catalogue L-07, key3 r10 on e5109212. A lands Cinematic; Look Assist is turned off through setSliders (a
// default-mask paste of a Look-Assist-off receipt, or Reset Receipt to an off default), which restores the baseline but
// never clears the outcome; ticking Look Assist on restores/captures the baseline again and the frame-ready step
// dedup_skips (the marker still names A), so nothing lands. The toggle-ON step is modelled from the handler's source:
// it marks only if the handler does. RED on e5109212 ("Look: Cinematic" over the ungraded baseline).
TEST(LookLabelPolicy, L07ToggleOnAfterASetSlidersOffNeverNamesTheStaleFlavor)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString handler = functionBody(mw, QStringLiteral("void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )"));
    const int onBranch = handler.indexOf(QStringLiteral("if( checked )"));
    ASSERT_TRUE(onBranch >= 0);
    const QString onSync = handler.mid(onBranch, handler.indexOf(QStringLiteral("auto applyAfterBaselineFrame ="), onBranch) - onBranch);
    const bool toggleOnMarks = onSync.contains(QStringLiteral("m_lookAssistFlavorOutcome.baselineRestored( ACTIVE_RECEIPT );"));

    int a = 0;
    const QString cinematic = QStringLiteral("cinematic");
    lookassist::LookAssistFlavorOutcome outcome;
    lookassist::LookAssistAppliedMarker applied;
    outcome.landed(&a, cinematic, false);   // open A: Cinematic lands
    applied.markApplied(&a);
    ASSERT_EQ(std::string("Look: Cinematic"), look_label::shownText(true, outcome, &a, cinematic).toStdString());
    // Paste B (Look Assist off) onto A: setSliders' off branch restores the baseline, no mark, no clear.
    ASSERT_EQ(std::string("Look: Off"), look_label::shownText(false, outcome, &a, cinematic).toStdString());
    // Tick Look Assist on: the handler restores/captures the baseline (and marks, from r11).
    if ( toggleOnMarks ) outcome.baselineRestored(&a);
    // The frame-ready step: isApplied(A) -> dedup_skip, nothing lands.
    ASSERT_TRUE(applied.isApplied(&a));
    const std::string shown = look_label::shownText(true, outcome, &a, cinematic).toStdString();
    printf("[L07] toggleOnMarks=%d shown=%s\n", toggleOnMarks ? 1 : 0, shown.c_str());
    ASSERT_EQ(std::string("Look: Baseline"), shown);
    ASSERT_TRUE(shown.find("Cinematic") == std::string::npos);
    // The receipt value setReceipt saves is unchanged (label only).
    ASSERT_EQ(std::string("cinematic"), outcome.receiptValue(&a, cinematic).toStdString());
}

// r11 -- L-07 wiring pin: the toggle-ON branch marks the active receipt AFTER its restore/capture and relabels, BEFORE
// setReceipt and the frame-ready re-grade (whose landing forgets the mark).
TEST(LookLabelWiring, L07ToggleOnRestoreIsLabelledBaseline)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString handler = functionBody(mw, QStringLiteral("void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )"));
    ASSERT_FALSE(handler.isEmpty());
    const int onBranch = handler.indexOf(QStringLiteral("if( checked )"));
    const int restore = handler.indexOf(QStringLiteral("restoreLookAssistBaseline( ACTIVE_RECEIPT );"), onBranch);
    const int capture = handler.indexOf(QStringLiteral("captureLookAssistBaseline( ACTIVE_RECEIPT );"), onBranch);
    const int marked = handler.indexOf(QStringLiteral("m_lookAssistFlavorOutcome.baselineRestored( ACTIVE_RECEIPT );"), onBranch);
    const int relabel = handler.indexOf(QStringLiteral("updateLookLabel();"), marked);
    const int save = handler.indexOf(QStringLiteral("setReceipt( ACTIVE_RECEIPT );"), onBranch);
    const int frameReady = handler.indexOf(QStringLiteral("auto applyAfterBaselineFrame ="), onBranch);
    printf("[L07W] onBranch=%d restore=%d capture=%d marked=%d relabel=%d save=%d frameReady=%d\n",
           onBranch, restore, capture, marked, relabel, save, frameReady);
    ASSERT_TRUE(onBranch >= 0);
    ASSERT_TRUE(restore > onBranch);
    ASSERT_TRUE(capture > restore);
    ASSERT_TRUE(marked > capture);      // after both branches: restored OR captured
    ASSERT_TRUE(relabel > marked);
    ASSERT_TRUE(save > relabel);
    ASSERT_TRUE(frameReady > save);
    ASSERT_EQ(1, handler.count(QStringLiteral("m_lookAssistFlavorOutcome.baselineRestored(")));
}

// POLICY-CALLSITE-BOOL-MUTATION-SMOKE-1 (master-merge round, 2026-11-02): the bool arguments the window passes to the
// policy are pinned at their one call site each, so flipping one (fullscreen vs. windowed, the show-in-fullscreen
// setting, or the status label's initial "look off" text) fails here rather than only on a venue leg.
TEST(LookLabelWiring, PolicyCallSitesPassTheWindowStateUnflipped)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_TRUE(update.contains(QStringLiteral(
        "look_label::fullscreenOverlayShown( ui->actionFullscreen->isChecked(), m_lookLabelShowInFullscreen );")));
    const QString overlayCall = QStringLiteral("look_label::fullscreenOverlayShown(");
    ASSERT_EQ(-1, mw.indexOf(overlayCall, mw.indexOf(overlayCall) + 1));
    const QString initialText =
        QStringLiteral("m_pLookLabelStatus->setText( look_label::text( /*lookAssistEnabled=*/false, QString() ) );");
    ASSERT_TRUE(mw.contains(initialText));
    const QString textCall = QStringLiteral("look_label::text(");
    ASSERT_EQ(-1, mw.indexOf(textCall, mw.indexOf(textCall) + 1));
}
