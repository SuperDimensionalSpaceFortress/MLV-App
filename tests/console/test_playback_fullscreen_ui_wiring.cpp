// Wiring/census test: pins CUDA-PLAYBACK-FULLSCREEN-UI-1 (unhide full screen for normal
// use). Verifies (1) the menu entry is reachable (no force-hide of actionFullscreen), (2)
// F11 is registered alongside the pre-existing Ctrl+F shortcut on Windows, (3) Escape only
// exits full screen -- it never enters it, and never fires unconditionally, (4) the
// pre-fullscreen maximized window state is captured and restored explicitly (showNormal()
// alone would lose it), and (5) the zoom-fit scene sizing (computeDisplaySceneGeometry) and
// the playback-smoke geometry verification both size from the window's own screen rather
// than always the primary one, for multi-monitor correctness.
// MainWindow.cpp needs a full GUI build (not linked into console_tests), so this test reads
// the source as text -- the call sites are pinned by markers, not by exercising a live
// window (mirrors test_playback_smoke_fullscreen_wiring.cpp's approach).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
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
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackFullscreenUiWiring, MenuEntryIsNotForceHidden)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(source.contains(QStringLiteral("ui->actionFullscreen->setVisible( false )")));
    // Still added to the View menu in the .ui -- nothing hides it after setupUi() runs.
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    ASSERT_TRUE(ui.contains(QStringLiteral("<addaction name=\"actionFullscreen\"/>")));
}

TEST(PlaybackFullscreenUiWiring, ContextMenuEntryStillOffersFullscreenWhileActive)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::pictureCustomContextMenuRequested"),
        QStringLiteral("void MainWindow::on_labelScope_customContextMenuRequested"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("if( ui->actionFullscreen->isChecked() )")));
    ASSERT_TRUE(body.contains(QStringLiteral("myMenu.addAction( ui->actionFullscreen );")));
}

TEST(PlaybackFullscreenUiWiring, F11IsRegisteredOnWindowsAlongsideTheExistingShortcut)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int at = source.indexOf(QStringLiteral("ui->actionFullscreen->setShortcuts("));
    ASSERT_TRUE(at >= 0);
    // Guarded to Windows: this pin only claims F11 for Q_OS_WIN, per the round brief.
    const int guardAt = source.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), at);
    const int guardEndAt = source.indexOf(QStringLiteral("#endif"), at);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(guardEndAt > at);
    const QString guarded = source.mid(guardAt, guardEndAt - guardAt);
    ASSERT_TRUE(guarded.contains(QStringLiteral("ui->actionFullscreen->shortcut()"))); // keeps Ctrl+F from the .ui
    ASSERT_TRUE(guarded.contains(QStringLiteral("QKeySequence( Qt::Key_F11 )")));

    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    const QString actionBlock = functionBody(ui,
        QStringLiteral("<action name=\"actionFullscreen\">"),
        QStringLiteral("<action name=\"actionCaching\">"));
    ASSERT_FALSE(actionBlock.isEmpty());
    ASSERT_TRUE(actionBlock.contains(QStringLiteral("<string>Ctrl+F</string>")));
}

TEST(PlaybackFullscreenUiWiring, EscapeOnlyExitsNeverEntersAndIsScopedToTheWindow)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int declAt = source.indexOf(QStringLiteral("new QShortcut( QKeySequence( Qt::Key_Escape ), this );"));
    ASSERT_TRUE(declAt >= 0);
    const int connectEndAt = source.indexOf(QStringLiteral("} );"), declAt);
    ASSERT_TRUE(connectEndAt > declAt);
    const QString block = source.mid(declAt, connectEndAt - declAt);
    // Only trigger()s (toggles off) when already checked -- never unconditionally, which
    // would let Escape enter full screen from elsewhere in the app.
    ASSERT_TRUE(block.contains(QStringLiteral("if( ui->actionFullscreen->isChecked() ) ui->actionFullscreen->trigger();")));
    // Default QShortcut context (Qt::WindowShortcut) -- no explicit setContext() call in
    // this block, so it stays confined to when this window is active and can't steal
    // Escape from modal dialogs.
    ASSERT_FALSE(block.contains(QStringLiteral("setContext(")));
}

TEST(PlaybackFullscreenUiWiring, MaximizedStateIsCapturedOnEntryAndRestoredOnExit)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::on_actionFullscreen_triggered( bool checked )"),
        QStringLiteral("void MainWindow::exportHandler( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("static bool windowWasMaximized;")));
    ASSERT_TRUE(body.contains(QStringLiteral("windowWasMaximized = isMaximized();")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( windowWasMaximized ) this->showMaximized();")));
    ASSERT_TRUE(body.contains(QStringLiteral("else this->showNormal();")));
    // Captured before showFullScreen() -- isMaximized() would already read false after it.
    const int captureAt = body.indexOf(QStringLiteral("windowWasMaximized = isMaximized();"));
    const int enterAt = body.indexOf(QStringLiteral("this->showFullScreen();"));
    ASSERT_TRUE(captureAt >= 0);
    ASSERT_TRUE(enterAt > captureAt);
}

TEST(PlaybackFullscreenUiWiring, ZoomFitSceneSizesFromTheWindowsOwnScreenNotAlwaysPrimary)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::computeDisplaySceneGeometry("),
        QStringLiteral("QSize MainWindow::"));
    ASSERT_FALSE(body.isEmpty());
    const int checkedAt = body.indexOf(QStringLiteral("if( ui->actionFullscreen->isChecked() )"));
    ASSERT_TRUE(checkedAt >= 0);
    const QString fullscreenBranch = body.mid(checkedAt, 700);
    ASSERT_TRUE(fullscreenBranch.contains(QStringLiteral("QScreen *fullscreenScreen = this->screen();")));
    ASSERT_TRUE(fullscreenBranch.contains(QStringLiteral("if( !fullscreenScreen ) fullscreenScreen = QApplication::primaryScreen();")));
    ASSERT_TRUE(fullscreenBranch.contains(QStringLiteral("actWidth = fullscreenScreen->size().width();")));
    ASSERT_TRUE(fullscreenBranch.contains(QStringLiteral("actHeight = fullscreenScreen->size().height();")));
}

TEST(PlaybackFullscreenUiWiring, SmokeGeometryVerificationUsesTheSameScreenChoice)
{
    // Consistency pin: the --gui-smoke-playback verification in enterPlaybackSmokeFullscreen()
    // must agree with computeDisplaySceneGeometry() on which screen "full screen" means, or a
    // window fullscreened on a non-primary monitor would spuriously fail verification.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::enterPlaybackSmokeFullscreen( void )"),
        QStringLiteral("void MainWindow::leavePlaybackSmokeFullscreen( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("QScreen *fullscreenScreen = this->screen();")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( !fullscreenScreen ) fullscreenScreen = QApplication::primaryScreen();")));
    ASSERT_TRUE(body.contains(QStringLiteral("const QSize screenSize = fullscreenScreen ? fullscreenScreen->size() : QSize();")));
    ASSERT_FALSE(body.contains(QStringLiteral("QApplication::primaryScreen()\n        ? QApplication::primaryScreen()")));
}

TEST(PlaybackFullscreenUiWiring, HeaderStillDeclaresTheAction)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void on_actionFullscreen_triggered(bool checked);")));
}
