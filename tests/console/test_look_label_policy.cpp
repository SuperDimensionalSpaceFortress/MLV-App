// LOOK-FULLSCREEN-PARITY-1: the "current look" label. Owner, 2026-10-15: "Full screen dont bypass the look. I just
// want text in the bottom right showing current look when in full screen and somewhere on GUI when windowed".
// The text, the corner placement and the hide setting are pure (LookLabelPolicy.h); the MainWindow / surface call
// sites are GUI/GL code not linked into console_tests, so they are pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

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
    ASSERT_TRUE(update.contains(QStringLiteral("look_label::appliedText(")));
    ASSERT_TRUE(update.contains(QStringLiteral("m_lookAssistFlavorOutcome.receiptValue( lookReceipt,")));
    ASSERT_TRUE(update.contains(QStringLiteral("m_lookAssistFlavorOutcome.fellBack( lookReceipt )")));
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
    // One source of truth: the receipt's switch, the checkbox and the flavor the grade uses.
    ASSERT_TRUE(update.contains(QStringLiteral("ACTIVE_RECEIPT->lookAssistEnabled()")));
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
