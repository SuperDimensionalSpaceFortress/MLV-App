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

#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
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
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackFullscreenUiWiring, MenuEntryIsNotForceHidden)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(source.contains(QStringLiteral("ui->actionFullscreen->setVisible( false )")));
    // Still added to the Playback menu (MainWindow.ui:291) -- nothing hides it after setupUi() runs.
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
    // PLAYER-STANDARD-SHORTCUT-MAP-1: F11 lives in the ONE player key table
    // (PlayerShortcutBinding.h bindings()), on the actionFullscreen row next to Ctrl+F, and
    // MainWindow.cpp no longer adds it in code.
    const QString helper = readRepoFile(QStringLiteral("platform/qt/PlayerShortcutBinding.h"));
    const QString f11 = QStringLiteral("QKeySequence( Qt::Key_F11 )");
    const int at = helper.indexOf(f11);
    ASSERT_TRUE(at >= 0);
    ASSERT_TRUE(helper.indexOf(QStringLiteral("Qt::Key_F11"), at + int(f11.size())) < 0);
    // Guarded to Windows: this pin only claims F11 for Q_OS_WIN, per the round brief.
    const int guardAt = helper.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), at);
    const int guardEndAt = helper.indexOf(QStringLiteral("#endif"), at);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(guardEndAt > at);
    ASSERT_TRUE(helper.indexOf(QStringLiteral("#endif"), guardAt) == guardEndAt);
    const int ctrlFAt = helper.lastIndexOf(
        QStringLiteral("QList<KeyBinding> fullscreen{ { QKeySequence( Qt::CTRL | Qt::Key_F ), KeyKind::Primary } };"), guardAt);
    ASSERT_TRUE(ctrlFAt >= 0); // keeps Ctrl+F, first (the key the menu shows)
    const int rowAt = helper.indexOf(QStringLiteral("QStringLiteral( \"actionFullscreen\" ), fullscreen"), guardEndAt);
    ASSERT_TRUE(rowAt > guardEndAt);

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(source.contains(QStringLiteral("ui->actionFullscreen->setShortcuts(")));
    ASSERT_FALSE(source.contains(QStringLiteral("Qt::Key_F11")));

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
    // must agree with computeDisplaySceneGeometry() on which screen "full screen" means (both
    // read this->screen(), not always primary), or a window fullscreened on a non-primary
    // monitor would spuriously fail verification. UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol
    // pre-review BLOCKER 3): enterPlaybackSmokeFullscreen() now takes the CHOSEN target and
    // re-queries this->screen() on every settle pass (never captured once up front) to verify
    // it actually landed there -- the window is moved to that same target BEFORE this call
    // (movePlaybackSmokeWindowToScreen(), runGuiPlaybackSmoke()'s only caller), so
    // this->screen() and computeDisplaySceneGeometry()'s own choice still converge once
    // verified=true.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::enterPlaybackSmokeFullscreen( QScreen *target )"),
        QStringLiteral("void MainWindow::leavePlaybackSmokeFullscreen( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("QScreen *presentationScreen = this->screen();")));
    ASSERT_TRUE(body.contains(QStringLiteral("presentationScreen = this->screen();")));
    ASSERT_TRUE(body.contains(QStringLiteral("const bool onTarget = ( presentationScreen == target );")));
}

TEST(PlaybackFullscreenUiWiring, PlayerShortcutsAreBoundToTheWindowAndKeepWindowScope)
{
    // PLAYER-FULLSCREEN-SHORTCUTS-LIVE-1: fullscreen hides the menu bar and toolbar, the only
    // visible carriers of the player actions where the menu bar is a widget (Windows, Linux; a
    // macOS native menu bar keeps carrying them), so MainWindow binds them to itself as well.
    // The behaviour is proven by gui_tests fullscreenLiveShortcuts_*; this pins the call site.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("#include \"PlayerShortcutBinding.h\"")));
    const int bindAt = source.indexOf(QStringLiteral("PlayerShortcutBinding::bindToWindow( this );"));
    ASSERT_TRUE(bindAt >= 0);
    ASSERT_TRUE(source.indexOf(QStringLiteral("PlayerShortcutBinding::bindToWindow( this );"), bindAt + 1) < 0);
    // Inside initGui(), which the constructor calls after ui->setupUi(this) -- the actions
    // must exist when it runs (bindToWindow() warns for any it cannot find).
    const QString initGui = functionBody(source,
        QStringLiteral("void MainWindow::initGui( void )"),
        QStringLiteral("void MainWindow::initLib( void )"));
    ASSERT_FALSE(initGui.isEmpty());
    ASSERT_TRUE(initGui.contains(QStringLiteral("PlayerShortcutBinding::bindToWindow( this );")));
    const int setupAt = source.indexOf(QStringLiteral("ui->setupUi(this);"));
    const int initGuiCallAt = source.indexOf(QStringLiteral("    initGui();"), setupAt);
    ASSERT_TRUE(setupAt >= 0);
    ASSERT_TRUE(initGuiCallAt > setupAt);

    // Window scope kept: the helper never changes the shortcut context. ApplicationShortcut
    // would fire while a non-modal dialog (StatusDialog) has focus.
    const QString helper = readRepoFile(QStringLiteral("platform/qt/PlayerShortcutBinding.h"));
    ASSERT_FALSE(helper.contains(QStringLiteral("setShortcutContext")));
    ASSERT_FALSE(helper.contains(QStringLiteral("ApplicationShortcut")));
    ASSERT_TRUE(helper.contains(QStringLiteral("window->addAction( action );")));
}

TEST(PlaybackFullscreenUiWiring, FocusGuardIsInstalledOnceRightAfterTheBinding)
{
    // PLAYER-STANDARD-SHORTCUT-MAP-1: the table binds navigation keys (Left/Right/Home/End/PgUp/
    // PgDown/Return/Enter); the guard keeps them with focused input widgets, the seek bar exempt.
    // Behaviour: gui_tests playerShortcutMap_focusGuard*; this pins the one install site.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString install = QStringLiteral(
        "qApp->installEventFilter( new PlayerShortcutBinding::FocusGuard( ui->horizontalSliderPosition, this ) );");
    const int installAt = source.indexOf(install);
    ASSERT_TRUE(installAt >= 0);
    ASSERT_TRUE(source.indexOf(install, installAt + 1) < 0);
    ASSERT_TRUE(source.indexOf(QStringLiteral("PlayerShortcutBinding::FocusGuard(")) == source.indexOf(install) + int(QStringLiteral("qApp->installEventFilter( new ").size()));
    const QString initGui = functionBody(source,
        QStringLiteral("void MainWindow::initGui( void )"),
        QStringLiteral("void MainWindow::initLib( void )"));
    ASSERT_FALSE(initGui.isEmpty());
    const int bindAt = initGui.indexOf(QStringLiteral("PlayerShortcutBinding::bindToWindow( this );"));
    ASSERT_TRUE(bindAt >= 0);
    ASSERT_TRUE(initGui.indexOf(install) > bindAt);
}

TEST(PlaybackFullscreenUiWiring, MainWindowSetsNoShortcutOnATableAction)
{
    // The live keys of a table action are exactly PlayerShortcutBinding::bindings(); a
    // setShortcut(s) call in MainWindow.cpp would be a second, hand-kept key list.
    const QString helper = readRepoFile(QStringLiteral("platform/qt/PlayerShortcutBinding.h"));
    const QString table = functionBody(helper,
        QStringLiteral("inline QList<ActionBinding> bindings()"),
        QStringLiteral("inline QList<QKeySequence> sequencesOf("));
    ASSERT_FALSE(table.isEmpty());
    QStringList names;
    QRegularExpressionMatchIterator it =
        QRegularExpression(QStringLiteral("QStringLiteral\\( \"(action\\w+)\" \\)")).globalMatch(table);
    while (it.hasNext()) names << it.next().captured(1);
    ASSERT_TRUE(names.size() >= 13);
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    for (const QString & name : names)
    {
        ASSERT_FALSE(source.contains(QStringLiteral("ui->%1->setShortcut(").arg(name)));
        ASSERT_FALSE(source.contains(QStringLiteral("ui->%1->setShortcuts(").arg(name)));
    }
}

TEST(PlaybackFullscreenUiWiring, OneSecondSeeksGoThroughTheOverflowSafeHelper)
{
    // Adversary pass 2 A1 (95d6cb1c): clip metadata can carry an accepted frame rate such as
    // 2147483648; qRound() of it (or position + step in int) overflows before the slider clamps.
    // Both seek slots compute the target in PlayerShortcutBinding::oneSecondSeekTarget()
    // (behaviour: gui_tests playerShortcutMap_oneSecondSeekTargetNeverOverflows).
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    for (const char *slot : { "void MainWindow::on_actionSeekBackwardOneSecond_triggered()",
                              "void MainWindow::on_actionSeekForwardOneSecond_triggered()" })
    {
        const QString body = functionBody(source, QString::fromLatin1(slot), QStringLiteral("\n}"));
        ASSERT_FALSE(body.isEmpty());
        ASSERT_TRUE(body.contains(QStringLiteral("PlayerShortcutBinding::oneSecondSeekTarget(")));
        ASSERT_FALSE(body.contains(QStringLiteral("qRound(")));
        ASSERT_TRUE(body.contains(QStringLiteral("ui->horizontalSliderPosition->minimum()")));
        ASSERT_TRUE(body.contains(QStringLiteral("ui->horizontalSliderPosition->maximum()")));
    }
}

TEST(PlaybackFullscreenUiWiring, HelpMenuOffersKeyboardShortcuts)
{
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    const QString menuHelp = functionBody(ui,
        QStringLiteral("<widget class=\"QMenu\" name=\"menuHelp\">"),
        QStringLiteral("</widget>"));
    ASSERT_FALSE(menuHelp.isEmpty());
    ASSERT_TRUE(menuHelp.contains(QStringLiteral("<addaction name=\"actionKeyboardShortcuts\"/>")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString slot = functionBody(source,
        QStringLiteral("void MainWindow::on_actionKeyboardShortcuts_triggered()"),
        QStringLiteral("\n}"));
    ASSERT_FALSE(slot.isEmpty());
    ASSERT_TRUE(slot.contains(QStringLiteral("PlayerShortcutBinding::createShortcutsDialog( this )")));
}

namespace
{

QStringList addactionNames(const QString & block)
{
    QStringList names;
    QRegularExpressionMatchIterator it =
        QRegularExpression(QStringLiteral("<addaction name=\"(\\w+)\"/>")).globalMatch(block);
    while (it.hasNext()) names << it.next().captured(1);
    return names;
}

} // namespace

TEST(PlayerToolbarTransportButtons, MainToolBarCarriesTheClusterInOrderAndMatchesTheHeaderList)
{
    // PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1 T1: literal oracle, then a drift guard against the ONE list.
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    const QString toolbar = functionBody(ui,
        QStringLiteral("<widget class=\"QToolBar\" name=\"mainToolBar\">"),
        QStringLiteral("</widget>"));
    ASSERT_FALSE(toolbar.isEmpty());
    const QStringList all = addactionNames(toolbar);
    const int first = all.indexOf(QStringLiteral("actionGoto_First_Frame"));
    ASSERT_TRUE(first >= 0);
    const QStringList cluster = all.mid(first);
    const QStringList oracle{
        QStringLiteral("actionGoto_First_Frame"), QStringLiteral("actionPreviousFrame"),
        QStringLiteral("actionPlay"), QStringLiteral("actionStop"), QStringLiteral("actionNextFrame"),
        QStringLiteral("actionGoto_Last_Frame"), QStringLiteral("actionLoop"), QStringLiteral("separator"),
        QStringLiteral("actionAudioOutput"), QStringLiteral("separator"), QStringLiteral("actionZoomFit"),
        QStringLiteral("actionZoom100"), QStringLiteral("actionFullscreen") };
    ASSERT_EQ(oracle.join(QStringLiteral(",")).toStdString(), cluster.join(QStringLiteral(",")).toStdString());

    const QString header = readRepoFile(QStringLiteral("platform/qt/PlayerTransportToolbar.h"));
    const QString list = functionBody(header,
        QStringLiteral("inline QStringList clusterActionNames()"),
        QStringLiteral("\n}"));
    ASSERT_FALSE(list.isEmpty());
    QStringList headerNames;
    QRegularExpressionMatchIterator it =
        QRegularExpression(QStringLiteral("QStringLiteral\\( \"(\\w+)\" \\)")).globalMatch(list);
    while (it.hasNext()) headerNames << it.next().captured(1);
    QStringList withoutSeparators = cluster;
    withoutSeparators.removeAll(QStringLiteral("separator"));
    ASSERT_EQ(withoutSeparators.join(QStringLiteral(",")).toStdString(), headerNames.join(QStringLiteral(",")).toStdString());
}

TEST(PlayerToolbarTransportButtons, PlaybackMenuOffersStopRightAfterPlayAndStopHasNoShortcut)
{
    // T2: K is reserved for PLAYER-JKL-SHUTTLE-1, so Stop stays unbound.
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    const QString menu = functionBody(ui,
        QStringLiteral("<widget class=\"QMenu\" name=\"menuPlayback\">"),
        QStringLiteral("<widget class=\"QMenu\" name=\"menuView\">"));
    ASSERT_FALSE(menu.isEmpty());
    const QStringList names = addactionNames(menu);
    const int play = names.indexOf(QStringLiteral("actionPlay"));
    ASSERT_TRUE(play >= 0);
    ASSERT_TRUE(play + 1 < names.size());
    ASSERT_EQ(std::string("actionStop"), names.at(play + 1).toStdString());
    const QString action = functionBody(ui,
        QStringLiteral("<action name=\"actionStop\">"),
        QStringLiteral("</action>"));
    ASSERT_FALSE(action.isEmpty());
    ASSERT_FALSE(action.contains(QStringLiteral("name=\"shortcut\"")));
}

TEST(PlayerToolbarTransportButtons, StopPausesFirstThenSeeksToCutIn)
{
    // T3: the user-pause form, then the clamped cut-in seek. Never the Goto First Frame toggle,
    // which jumps to 0 when the position already is cut in.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::on_actionStop_triggered()"),
        QStringLiteral("\n}"));
    ASSERT_FALSE(body.isEmpty());
    const int setChecked = body.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    const int paused = body.indexOf(QStringLiteral("on_actionPlay_triggered( false );"));
    const int seek = body.indexOf(QStringLiteral("horizontalSliderPosition->setValue("));
    ASSERT_TRUE(setChecked >= 0);
    ASSERT_TRUE(paused > setChecked);
    ASSERT_TRUE(seek > paused);
    ASSERT_TRUE(body.contains(QStringLiteral("PlayerTransportToolbar::stopTargetFrame( ui->spinBoxCutIn->value()")));
    ASSERT_FALSE(body.contains(QStringLiteral("on_actionGoto_First_Frame_triggered")));
}

TEST(PlayerToolbarTransportButtons, FinishRunsOnceAtTheEndOfInitGuiAfterTheLiveBinding)
{
    // T4: tooltips read the live keys, so finish() must run after bindToWindow().
    const QString call = QStringLiteral("PlayerTransportToolbar::finish( ui->mainToolBar, this );");
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(1, int(source.count(call)));
    ASSERT_EQ(1, int(source.count(QStringLiteral("PlayerTransportToolbar::finish("))));
    const QString initGui = functionBody(source,
        QStringLiteral("void MainWindow::initGui( void )"),
        QStringLiteral("void MainWindow::initLib( void )"));
    ASSERT_FALSE(initGui.isEmpty());
    const int callAt = initGui.indexOf(call);
    ASSERT_TRUE(callAt >= 0);
    const int bindAt = initGui.indexOf(QStringLiteral("PlayerShortcutBinding::bindToWindow( this );"));
    const int guardAt = initGui.indexOf(QStringLiteral(
        "qApp->installEventFilter( new PlayerShortcutBinding::FocusGuard( ui->horizontalSliderPosition, this ) );"));
    ASSERT_TRUE(bindAt >= 0);
    ASSERT_TRUE(guardAt > bindAt);
    ASSERT_TRUE(callAt > guardAt);

    // No other app source calls it.
    for (const char *base : { "platform/qt", "src" })
    {
        const QString root = repo_file_path(QString::fromLatin1(base));
        ASSERT_FALSE(root.isEmpty());
        QDirIterator files(root, QStringList{ QStringLiteral("*.cpp"), QStringLiteral("*.h") },
                           QDir::Files, QDirIterator::Subdirectories);
        while (files.hasNext())
        {
            const QString path = files.next();
            if (path.endsWith(QStringLiteral("/MainWindow.cpp")) || path.endsWith(QStringLiteral("/PlayerTransportToolbar.h"))) continue;
            if (path.contains(QStringLiteral("/build")) || QFileInfo(path).fileName().startsWith(QStringLiteral("ui_"))
                || QFileInfo(path).fileName().startsWith(QStringLiteral("moc_"))) continue;
            QFile file(path);
            ASSERT_TRUE(file.open(QIODevice::ReadOnly));
            ASSERT_FALSE(QString::fromUtf8(file.readAll()).contains(QStringLiteral("PlayerTransportToolbar::finish(")));
        }
    }
}

TEST(PlaybackFullscreenUiWiring, HeaderStillDeclaresTheAction)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void on_actionFullscreen_triggered(bool checked);")));
}
