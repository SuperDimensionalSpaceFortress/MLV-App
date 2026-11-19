// PLAYER-CURSOR-AUTOHIDE-1: wiring pins for the fullscreen cursor auto-hide. MainWindow.cpp needs
// a full GUI build (not linked into console_tests), so the call sites are pinned as source text,
// following test_playback_fullscreen_ui_wiring.cpp. This file deliberately includes neither
// ViewerIdlePolicy.h nor ViewerCursorSurfaces.h, so it compiles on a base without them (RED proof).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
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

// The body of one function, from its definition to its closing brace in column 0.
QString functionBodyToBrace(const QString & source, const QString & signature)
{
    const int start = source.indexOf(signature);
    if (start < 0) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    if (end <= start) return QString();
    return source.mid(start, end - start);
}

} // namespace

// W1: one call in the application filter, after the Resize branch and as the last statement
// before the filter's return, so every existing branch is unchanged and every event is seen.
TEST(ViewerIdleWiring, EventFilterNotesEveryEventOnceBeforeItsReturn)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::eventFilter(QObject *watched, QEvent *event)"),
        QStringLiteral("bool MainWindow::playbackPolicyActive( void ) const"));
    ASSERT_FALSE(body.isEmpty());
    const QString call = QStringLiteral("viewerIdleNoteEvent( watched, event );");
    ASSERT_EQ(1, int(body.count(call)));
    const int resizeAt = body.indexOf(QStringLiteral("QEvent::Resize"));
    const int callAt = body.indexOf(call);
    ASSERT_TRUE(resizeAt >= 0);
    ASSERT_TRUE(callAt > resizeAt);

    const QStringList lines = body.split(QLatin1Char('\n'));
    int returnLine = -1;
    for (int i = 0; i < lines.size(); ++i)
    {
        if (lines.at(i).trimmed() == QStringLiteral("return QWidget::eventFilter(watched, event);")) returnLine = i;
    }
    ASSERT_TRUE(returnLine > 0);
    if (returnLine > 0)
    {
        ASSERT_TRUE(lines.at(returnLine - 1).trimmed().startsWith(call));
    }
}

// W2: start/stop is QAction::toggled on actionFullscreen; arming any of the three tools restores
// the cursor at once. Wired in the constructor after the application filter is installed.
TEST(ViewerIdleWiring, ConstructorConnectsFullscreenAndToolToggles)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString block = functionBody(source,
        QStringLiteral("qApp->installEventFilter( this );"),
        QStringLiteral("//Set bools for draw rules"));
    ASSERT_FALSE(block.isEmpty());
    ASSERT_TRUE(block.contains(QStringLiteral("m_viewerIdleTimer->setSingleShot( true );")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( m_viewerIdleTimer, &QTimer::timeout")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->actionFullscreen, &QAction::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("viewerIdleFullscreenChanged( on );")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->actionWhiteBalancePicker, &QAction::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->toolButtonBadPixelsSearchMethodEdit, &QAbstractButton::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->toolButtonGradientPaint, &QAbstractButton::toggled")));
    ASSERT_EQ(3, int(block.count(QStringLiteral("viewerIdleRestore();"))));
}

// W3: on_actionFullscreen_triggered is untouched (the toggled path owns start/stop and the
// restore on leaving fullscreen); no app-wide override cursor anywhere in MainWindow.cpp.
TEST(ViewerIdleWiring, FullscreenSlotUntouchedAndLeavingRestoresWithoutOverrideCursor)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString slot = functionBody(source,
        QStringLiteral("void MainWindow::on_actionFullscreen_triggered( bool checked )"),
        QStringLiteral("void MainWindow::exportHandler( void )"));
    ASSERT_FALSE(slot.isEmpty());
    ASSERT_FALSE(slot.contains(QStringLiteral("viewerIdle")));
    ASSERT_FALSE(source.contains(QStringLiteral("setOverrideCursor")));

    const QString changed = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleFullscreenChanged( bool on )"));
    ASSERT_FALSE(changed.isEmpty());
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerIdleTimer->stop();")));
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerIdle.forceShown()")));
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerCursorHider.restore( viewerIdleSurfaces() );")));
}

// W4: the hide gate reads real window state and all three tools; the surfaces are read fresh,
// including the GL display window (never a cached pointer).
TEST(ViewerIdleWiring, InputsReadWindowStateAndToolsAndSurfacesReadGlWindowFresh)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString inputs = functionBodyToBrace(source,
        QStringLiteral("viewer_idle::Inputs MainWindow::viewerIdleInputs( void ) const"));
    ASSERT_FALSE(inputs.isEmpty());
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->actionFullscreen->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("isFullScreen()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("isActiveWindow()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("QApplication::activePopupWidget()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("QApplication::activeModalWidget()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->actionWhiteBalancePicker->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->toolButtonBadPixelsSearchMethodEdit->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->toolButtonGradientPaint->isChecked()")));

    const QString surfaces = functionBodyToBrace(source,
        QStringLiteral("viewer_idle::Surfaces MainWindow::viewerIdleSurfaces( void ) const"));
    ASSERT_FALSE(surfaces.isEmpty());
    ASSERT_TRUE(surfaces.contains(QStringLiteral("ui->graphicsView->viewport()")));
    ASSERT_TRUE(surfaces.contains(QStringLiteral("GpuDisplayWindow::activeWindow()")));

    // Windowed mode costs one bool test: the note returns at once unless the controller is fullscreen.
    const QString note = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )"));
    ASSERT_FALSE(note.isEmpty());
    ASSERT_TRUE(note.contains(QStringLiteral("if( !m_viewerIdle.fullscreen() ) return;")));
    ASSERT_TRUE(note.contains(QStringLiteral("m_viewerIdlePointer.seed( QCursor::pos() );")));

    // Adversary A2: every tick also asks the OS pointer, so movement during a wait that excludes user input
    // (no mouse event delivered, timers still run) keeps / brings the cursor back; Hide and Show re-arm.
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    ASSERT_TRUE(timeout.contains(QStringLiteral("in.pointerMoved = m_viewerIdlePointer.moved( QCursor::pos() );")));
    ASSERT_TRUE(timeout.contains(QStringLiteral("case viewer_idle::Tick::Show:")));
    ASSERT_EQ(3, int(timeout.count(QStringLiteral("m_viewerIdleTimer->start( ms );"))));
}
