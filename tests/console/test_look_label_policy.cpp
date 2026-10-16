// LOOK-FULLSCREEN-PARITY-1: the "current look" label. Owner, 2026-10-15: "Full screen dont bypass the look. I just
// want text in the bottom right showing current look when in full screen and somewhere on GUI when windowed".
// The text, the corner placement and the hide setting are pure (LookLabelPolicy.h); the MainWindow / surface call
// sites are GUI/GL code not linked into console_tests, so they are pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/LookLabelPolicy.h"

#include <QFile>
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
    // LOOK-PRESET-DROPDOWN-1 replaced the windowed status-bar label with the look dropdown, synced from here.
    ASSERT_TRUE(update.contains(QStringLiteral("syncLookPresetDropdown( lookOn, flavorName );")));

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
