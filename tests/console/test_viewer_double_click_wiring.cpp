// PLAYER-DOUBLECLICK-FULLSCREEN-1: source pins for the viewer double-click wiring in MainWindow.
// MainWindow.cpp needs a full GUI build (not linked into console_tests), so this reads the sources
// as text, the same way test_playback_fullscreen_ui_wiring.cpp does. It deliberately includes no
// new header, so it compiles (and fails as assertions) on a base without the change.
// W1 one handler, first in eventFilter, triggers the existing fullscreen action and swallows.
// W2 the Playback-menu toggle: checkable, checked by default, no shortcut.
// W3 the setting is read at start-up and written by the slot.
// W4 the state reader covers all four tools and guards the uninitialised gradient pointer.
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
    if (at < 0) return QString();
    const int next = source.indexOf(nextSignature, at + signature.size());
    if (next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(ViewerDoubleClickWiring, W1_EventFilterHandlesTheDoubleClickOnceAndSwallowsIt)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::eventFilter"),
        QStringLiteral("bool MainWindow::"));
    ASSERT_FALSE(body.isEmpty());

    const QString call = QStringLiteral("viewer_dblclick::filterEvent( m_viewerDoubleClickGate, watched, event, ui->graphicsView->viewport(),");
    const int callAt = body.indexOf(call);
    ASSERT_TRUE(callAt >= 0);
    ASSERT_TRUE(body.indexOf(call, callAt + 1) < 0); // exactly once
    ASSERT_TRUE(source.indexOf(call, source.indexOf(call) + 1) < 0); // and nowhere else

    // Ahead of the existing MouseMove branch, and after the idle-wait release.
    const int idleAt = body.indexOf(QStringLiteral("endGuiPresentIdleWait();"));
    const int moveAt = body.indexOf(QStringLiteral("QEvent::MouseMove"));
    ASSERT_TRUE(idleAt >= 0);
    ASSERT_TRUE(idleAt < callAt);
    ASSERT_TRUE(moveAt > callAt);

    // Both display paths are named, and the state is read lazily from the member reader.
    const QString block = body.mid(callAt, moveAt - callAt);
    ASSERT_TRUE(block.contains(QStringLiteral("GpuDisplayWindow::activeWindow()")));
    // r2 pass 4 A1: the reader is handed the event's own target and position, never the live cursor.
    ASSERT_TRUE(block.contains(QStringLiteral("[this]( const QObject *target, const QPointF &pos ) { return viewerDoubleClickState( target, pos ); }")));
    const int triggerAt = block.indexOf(QStringLiteral("ui->actionFullscreen->trigger();"));
    const int returnAt = block.indexOf(QStringLiteral("return true;"));
    ASSERT_TRUE(triggerAt >= 0);
    ASSERT_TRUE(returnAt > triggerAt);

    // No play/pause on a click: the filter never touches the play action.
    ASSERT_FALSE(body.contains(QStringLiteral("actionPlay")));
    ASSERT_TRUE(source.contains(QStringLiteral("#include \"ViewerDoubleClickFilter.h\"")));
}

TEST(ViewerDoubleClickWiring, W2_PlaybackMenuToggleIsCheckableCheckedAndHasNoShortcut)
{
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    const QString lookLabel = QStringLiteral("<addaction name=\"actionShowLookLabelInFullscreen\"/>");
    const int lookAt = ui.indexOf(lookLabel);
    ASSERT_TRUE(lookAt >= 0);
    const int nextAddAt = ui.indexOf(QStringLiteral("<addaction "), lookAt + lookLabel.size());
    ASSERT_TRUE(nextAddAt > lookAt);
    ASSERT_TRUE(ui.mid(nextAddAt).startsWith(QStringLiteral("<addaction name=\"actionDoubleClickViewerFullscreen\"/>")));

    // Inside menuPlayback: the menu's own closing tag (its indent level; nested submenus close
    // deeper) comes after the new entry.
    const QString menuDecl = QStringLiteral("\n   <widget class=\"QMenu\" name=\"menuPlayback\">");
    const int menuAt = ui.lastIndexOf(menuDecl, nextAddAt);
    ASSERT_TRUE(menuAt >= 0);
    ASSERT_TRUE(ui.indexOf(QStringLiteral("\n   </widget>"), menuAt + menuDecl.size()) > nextAddAt);

    const QString action = functionBody(ui,
        QStringLiteral("<action name=\"actionDoubleClickViewerFullscreen\">"),
        QStringLiteral("</action>"));
    ASSERT_FALSE(action.isEmpty());
    ASSERT_TRUE(action.contains(QStringLiteral("<property name=\"checkable\">\n    <bool>true</bool>")));
    ASSERT_TRUE(action.contains(QStringLiteral("<property name=\"checked\">\n    <bool>true</bool>")));
    ASSERT_TRUE(action.contains(QStringLiteral("<string>Double-Click Viewer Toggles Fullscreen</string>")));
    ASSERT_FALSE(action.contains(QStringLiteral("name=\"shortcut\"")));
}

TEST(ViewerDoubleClickWiring, W3_SettingIsReadAtStartAndWrittenByTheSlot)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString init = functionBody(source,
        QStringLiteral("void MainWindow::initPlaybackQualityFromSettings( void )"),
        QStringLiteral("void MainWindow::"));
    ASSERT_FALSE(init.isEmpty());
    const int readAt = init.indexOf(QStringLiteral("m_doubleClickViewerFullscreen = set.value( viewer_dblclick::kSettingsKey(), viewer_dblclick::kSettingsDefault() ).toBool();"));
    const int checkAt = init.indexOf(QStringLiteral("ui->actionDoubleClickViewerFullscreen->setChecked( m_doubleClickViewerFullscreen );"));
    ASSERT_TRUE(readAt >= 0);
    ASSERT_TRUE(checkAt > readAt);
    // Read through the automation-aware settings store this function opens.
    ASSERT_TRUE(init.contains(QStringLiteral("auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;")));

    const QString slot = functionBody(source,
        QStringLiteral("void MainWindow::on_actionDoubleClickViewerFullscreen_triggered()"),
        QStringLiteral("void MainWindow::"));
    ASSERT_FALSE(slot.isEmpty());
    ASSERT_TRUE(slot.contains(QStringLiteral("m_doubleClickViewerFullscreen = ui->actionDoubleClickViewerFullscreen->isChecked();")));
    ASSERT_TRUE(slot.contains(QStringLiteral("automation_settings::openAppSettings()")));
    ASSERT_TRUE(slot.contains(QStringLiteral("set.setValue( viewer_dblclick::kSettingsKey(), m_doubleClickViewerFullscreen );")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void on_actionDoubleClickViewerFullscreen_triggered();")));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_doubleClickViewerFullscreen = true;")));
}

TEST(ViewerDoubleClickWiring, W4_StateReaderCoversEveryToolAndGuardsTheGradientPointer)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("viewer_dblclick::ViewerState MainWindow::viewerDoubleClickState( const QObject *target, const QPointF &pos ) const"),
        QStringLiteral("\n}\n"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("s.enabled = m_doubleClickViewerFullscreen;")));
    ASSERT_TRUE(body.contains(QStringLiteral("s.clipLoaded = m_fileLoaded;")));
    ASSERT_TRUE(body.contains(QStringLiteral("s.wbArmed = ui->actionWhiteBalancePicker->isChecked();")));
    ASSERT_TRUE(body.contains(QStringLiteral("s.bpArmed = ui->toolButtonBadPixelsSearchMethodEdit->isChecked();")));
    ASSERT_TRUE(body.contains(QStringLiteral("s.gradientPaintArmed = ui->toolButtonGradientPaint->isChecked();")));

    const int guardAt = body.indexOf(QStringLiteral("if( m_fileLoaded && m_pGradientElement && m_pGradientElement->gradientGraphicsElement() )"));
    // Adversary r1 pass 3 A1: the handle counts only while the QGraphicsView is the visible display.
    // r2 pass 4 A1: hit-tested at the event's position on the viewport (QGraphicsView::items takes
    // viewport coordinates), never isUnderMouse(), which reads the live OS cursor.
    const int hoverAt = body.indexOf(QStringLiteral("s.gradientHandleHovered = viewer_dblclick::gradientHandleClaims( onView, handle->isVisible(), onView && ui->graphicsView->items( pos.toPoint() ).contains( handle ) );"));
    ASSERT_TRUE(body.contains(QStringLiteral("const bool onView = target == ui->graphicsView->viewport() && ui->graphicsView->isVisible();")));
    ASSERT_FALSE(body.contains(QStringLiteral("isUnderMouse")));
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(hoverAt > guardAt);
    // The gradient pointer is never dereferenced ahead of the guard.
    const int firstDeref = body.indexOf(QStringLiteral("m_pGradientElement->"));
    ASSERT_TRUE(firstDeref > guardAt);
}
