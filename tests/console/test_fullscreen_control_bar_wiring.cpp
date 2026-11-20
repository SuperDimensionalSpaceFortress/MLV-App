// PLAYER-FULLSCREEN-CONTROL-BAR-1: wiring pins for the fullscreen control bar. MainWindow.cpp and GpuDisplayWindow.cpp
// need a full GUI build (not linked into console_tests), so the call sites are pinned as source text, following
// test_viewer_idle_wiring.cpp. This file includes no bar header, so it compiles on a base without them (RED proof:
// there every assertion on the new call sites fails).
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
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream stream(&file);
    return stream.readAll();
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

// The first statement after the opening brace of a function body.
QString firstStatement(const QString & body)
{
    const int brace = body.indexOf(QLatin1Char('{'));
    if (brace < 0) return QString();
    const QStringList lines = body.mid(brace + 1).split(QLatin1Char('\n'));
    for (const QString & line : lines)
    {
        const QString t = line.trimmed();
        if (!t.isEmpty() && !t.startsWith(QStringLiteral("//"))) return t;
    }
    return QString();
}

bool mentionsBar(const QString & text)
{
    return text.contains(QStringLiteral("fullscreenBar")) || text.contains(QStringLiteral("ControlBar"))
        || text.contains(QStringLiteral("controlBar")) || text.contains(QStringLiteral("fullscreen_bar"));
}

} // namespace

// C1: in the application filter the GL-path bar routing comes right after endGuiPresentIdleWait() and before the
// viewer's double-click toggle, and the routing notes the idle clock before it swallows the event.
TEST(FullscreenControlBarWiring, EventFilterRoutesTheBarBeforeTheViewerDoubleClick)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString filter = functionBodyToBrace(source,
        QStringLiteral("bool MainWindow::eventFilter(QObject *watched, QEvent *event)"));
    ASSERT_FALSE(filter.isEmpty());
    const QString route = QStringLiteral("if( fullscreenBarRouteGpuEvent( watched, event ) ) return true;");
    ASSERT_EQ(1, int(filter.count(route)));
    const int routeAt = filter.indexOf(route);
    const int idleWaitAt = filter.indexOf(QStringLiteral("endGuiPresentIdleWait();"));
    const int dblAt = filter.indexOf(QStringLiteral("viewer_dblclick::filterEvent"));
    ASSERT_TRUE(idleWaitAt >= 0);
    ASSERT_TRUE(dblAt > 0);
    ASSERT_TRUE(routeAt > idleWaitAt);
    ASSERT_TRUE(routeAt < dblAt);

    const QString gpu = functionBodyToBrace(source,
        QStringLiteral("bool MainWindow::fullscreenBarRouteGpuEvent( QObject *watched, QEvent *event )"));
    ASSERT_FALSE(gpu.isEmpty());
    ASSERT_TRUE(gpu.contains(QStringLiteral("GpuDisplayWindow::activeWindow()")));
    ASSERT_TRUE(gpu.contains(QStringLiteral("watched != gl")));
    ASSERT_TRUE(gpu.contains(QStringLiteral("gl->devicePixelRatio()")));
    const int noteAt = gpu.indexOf(QStringLiteral("viewerIdleNoteEvent( watched, event );"));
    ASSERT_TRUE(noteAt > 0);
    ASSERT_TRUE(gpu.indexOf(QStringLiteral("return true;"), noteAt) > noteAt);

    // The actions are the existing ones.
    const QString input = functionBodyToBrace(source,
        QStringLiteral("bool MainWindow::fullscreenBarInput( const fullscreen_bar::Layout &layout, QEvent *event, const QPointF &devicePos, bool fromWidget )"));
    ASSERT_FALSE(input.isEmpty());
    ASSERT_TRUE(input.contains(QStringLiteral("ui->actionPlay->trigger();")));
    ASSERT_TRUE(input.contains(QStringLiteral("ui->actionPreviousFrame->trigger();")));
    ASSERT_TRUE(input.contains(QStringLiteral("ui->actionNextFrame->trigger();")));
    ASSERT_TRUE(input.contains(QStringLiteral("ui->actionFullscreen->trigger();")));
    ASSERT_TRUE(input.contains(QStringLiteral("slider->setValue( slider->minimum() + fullscreen_bar::seekFrameAt(")));

    // The full argument lists of both policy calls: the GL route claims only while the bar is shown (a hidden bar
    // never swallows a viewer click), and the widget path, which only receives events on the bar, maps directly.
    ASSERT_EQ(1, int(input.count(QStringLiteral(
        "else if( !fullscreen_bar::route( &m_fullscreenBarInput, fullscreenBarShown(), event->type(), button, buttons, part, &action ) ) return false;"))));
    ASSERT_EQ(1, int(input.count(QStringLiteral(
        "if( fromWidget ) action = fullscreen_bar::onMouse( &m_fullscreenBarInput, event->type(), button, buttons, part );"))));
}

// C2: paintGL draws the bar on the line after the look label (before the capture readback), drawControlBar returns at
// once while hidden, and the context teardown deletes the bar texture after the look label's.
TEST(FullscreenControlBarWiring, PaintGlDrawsTheBarAfterTheLookLabelAndTeardownDeletesIt)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString paint = functionBodyToBrace(source, QStringLiteral("void GpuDisplayWindow::paintGL()"));
    ASSERT_FALSE(paint.isEmpty());
    const QStringList lines = paint.split(QLatin1Char('\n'));
    int labelLine = -1;
    for (int i = 0; i < lines.size(); ++i)
    {
        if (lines.at(i).trimmed() == QStringLiteral("drawLookLabel(fbw, fbh);")) labelLine = i;
    }
    ASSERT_TRUE(labelLine > 0);
    ASSERT_TRUE(labelLine + 1 < lines.size());
    ASSERT_TRUE(lines.at(labelLine + 1).trimmed().startsWith(QStringLiteral("drawControlBar(fbw, fbh);")));
    ASSERT_EQ(1, int(paint.count(QStringLiteral("drawControlBar(fbw, fbh);"))));
    // Before the capture readback (the last test of the flag; the first is the no-frame branch).
    ASSERT_TRUE(paint.indexOf(QStringLiteral("drawControlBar(fbw, fbh);")) < paint.lastIndexOf(QStringLiteral("if ( m_captureReadbackRequested )\n")));

    const QString draw = functionBodyToBrace(source, QStringLiteral("void GpuDisplayWindow::drawControlBar(int fbw, int fbh)"));
    ASSERT_FALSE(draw.isEmpty());
    ASSERT_TRUE(firstStatement(draw) == QStringLiteral("if ( !m_controlBar.shown() ) return;"));
    // While playing no update() is asked for a bar change.
    const QString state = functionBodyToBrace(source,
        QStringLiteral("bool GpuDisplayWindow::setControlBarStateIfActive(bool shown, const fullscreen_bar::Feed &feed, qint64 nowMs)"));
    ASSERT_FALSE(state.isEmpty());
    ASSERT_EQ(1, int(state.count(QStringLiteral("update()"))));
    ASSERT_TRUE(state.contains(QStringLiteral("&& !feed.playing ) win->update();")));

    // The spec named it releaseGlResources; in this file the context teardown is cleanupGLResources.
    const QString cleanup = functionBodyToBrace(source, QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"));
    ASSERT_FALSE(cleanup.isEmpty());
    const int labelDelete = cleanup.indexOf(QStringLiteral("delete m_lookLabelTexture;"));
    const int barDelete = cleanup.indexOf(QStringLiteral("delete m_controlBarTexture;"));
    ASSERT_TRUE(labelDelete > 0);
    ASSERT_TRUE(barDelete > labelDelete);
    ASSERT_TRUE(cleanup.contains(QStringLiteral("m_controlBar.dropRaster();")));
}

// C3: one idle clock. Exactly one idle QTimer; the bar code creates no timer; the hide gate holds on the bar.
TEST(FullscreenControlBarWiring, OneIdleClockNoSecondTimer)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(1, int(source.count(QStringLiteral("m_viewerIdleTimer = new QTimer"))));
    const QString widget = readRepoFile(QStringLiteral("platform/qt/FullscreenControlBar.cpp"))
                         + readRepoFile(QStringLiteral("platform/qt/FullscreenControlBar.h"))
                         + readRepoFile(QStringLiteral("platform/qt/FullscreenControlBarPolicy.h"));
    ASSERT_FALSE(widget.isEmpty());
    ASSERT_FALSE(widget.contains(QStringLiteral("QTimer")));
    ASSERT_FALSE(widget.contains(QStringLiteral("startTimer")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/ViewerIdlePolicy.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool overlayHeld = false;")));
    ASSERT_TRUE(header.contains(QStringLiteral("&& !i.overlayHeld")));

    const QString inputs = functionBodyToBrace(source,
        QStringLiteral("viewer_idle::Inputs MainWindow::viewerIdleInputs( void ) const"));
    ASSERT_TRUE(inputs.contains(QStringLiteral("in.overlayHeld = fullscreenBarHeld();")));

    // Every cursor show/hide of #855 goes through viewerIdleApplyShown, which moves the bar with it.
    const QString apply = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleApplyShown( bool shown, bool byActivity )"));
    ASSERT_FALSE(apply.isEmpty());
    ASSERT_TRUE(apply.contains(QStringLiteral("m_viewerCursorHider.restore( viewerIdleSurfaces() );")));
    ASSERT_TRUE(apply.contains(QStringLiteral("m_viewerCursorHider.hide( viewerIdleSurfaces() );")));
    ASSERT_TRUE(apply.contains(QStringLiteral("fullscreenBarSetShown( m_viewerIdle.fullscreen() && shown")));
    ASSERT_EQ(2, int(source.count(QStringLiteral("m_viewerCursorHider."))));
    const char *sites[] = {
        "void MainWindow::viewerIdleTimeout( void )",
        "void MainWindow::viewerIdleRestore( void )",
        "void MainWindow::viewerIdleFullscreenChanged( bool on )",
        "void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )",
    };
    for (const char *site : sites)
    {
        const QString body = functionBodyToBrace(source, QString::fromLatin1(site));
        ASSERT_FALSE(body.isEmpty());
        ASSERT_TRUE(body.contains(QStringLiteral("viewerIdleApplyShown(")));
    }
    // A gate that forces the cursor back never shows a hidden bar; only real movement does.
    const QString timeout = functionBodyToBrace(source, QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_TRUE(timeout.contains(QStringLiteral("viewerIdleApplyShown( true, in.pointerMoved );")));
    ASSERT_TRUE(timeout.contains(QStringLiteral("viewerIdleApplyShown( false, false );")));
    const QString restore = functionBodyToBrace(source, QStringLiteral("void MainWindow::viewerIdleRestore( void )"));
    ASSERT_TRUE(restore.contains(QStringLiteral("viewerIdleApplyShown( true, false );")));
}

// C4: zero cost while hidden. The per-frame feed is connected on show and disconnected on hide; leaving fullscreen
// hides the bar and emits the one summary line (the destructor too, if not yet emitted).
TEST(FullscreenControlBarWiring, FeedConnectedOnlyWhileShownAndSummaryOnExit)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString set = functionBodyToBrace(source, QStringLiteral("void MainWindow::fullscreenBarSetShown( bool shown )"));
    ASSERT_FALSE(set.isEmpty());
    ASSERT_TRUE(set.contains(QStringLiteral("m_fullscreenBarSliderFeed = connect( ui->horizontalSliderPosition, &QSlider::valueChanged")));
    ASSERT_TRUE(set.contains(QStringLiteral("m_fullscreenBarPlayFeed = connect( ui->actionPlay, &QAction::toggled")));
    ASSERT_TRUE(set.contains(QStringLiteral("disconnect( m_fullscreenBarSliderFeed );")));
    ASSERT_TRUE(set.contains(QStringLiteral("disconnect( m_fullscreenBarPlayFeed );")));
    ASSERT_EQ(1, int(source.count(QStringLiteral("&QSlider::valueChanged, this, [this]( int ) { fullscreenBarFeed(); }"))));
    // Drop-frame playback moves the slider with its signals blocked: each presented frame is fed through
    // drawFrameNumberLabel instead, behind a flag that is set only while the bar is shown.
    const int showAt = set.indexOf(QStringLiteral("m_fullscreenBarFrameFeed = true;"));
    const int hideAt = set.indexOf(QStringLiteral("m_fullscreenBarFrameFeed = false;"));
    ASSERT_TRUE(showAt > 0);
    ASSERT_TRUE(hideAt > set.indexOf(QStringLiteral("else")));
    ASSERT_EQ(2, int(source.count(QStringLiteral("m_fullscreenBarFrameFeed = "))));
    const QString label = functionBodyToBrace(source, QStringLiteral("void MainWindow::drawFrameNumberLabel( int frameIndex )"));
    ASSERT_TRUE(label.contains(QStringLiteral("if( m_fullscreenBarFrameFeed ) fullscreenBarFeed( frameIndex );")));

    const QString feed = functionBodyToBrace(source, QStringLiteral("void MainWindow::fullscreenBarFeed( int frameIndex )"));
    ASSERT_TRUE(feed.contains(QStringLiteral("m_pTimeCodeImage->getTimeCodeFromFps(")));
    ASSERT_TRUE(feed.contains(QStringLiteral("GpuDisplayWindow::setControlBarStateIfActive( true, feed,")));

    const QString changed = functionBodyToBrace(source, QStringLiteral("void MainWindow::viewerIdleFullscreenChanged( bool on )"));
    const int elseAt = changed.indexOf(QStringLiteral("else"));
    ASSERT_TRUE(changed.indexOf(QStringLiteral("fullscreenBarSetShown( false );")) > elseAt);
    ASSERT_TRUE(changed.indexOf(QStringLiteral("if( wasFullscreen ) fullscreenBarEmitSummary();")) > elseAt);
    const QString dtor = functionBodyToBrace(source, QStringLiteral("MainWindow::~MainWindow()"));
    ASSERT_TRUE(dtor.contains(QStringLiteral("if( !m_fullscreenBarSummaryEmitted ) fullscreenBarEmitSummary();")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/FullscreenControlBarPolicy.h"));
    ASSERT_TRUE(policy.contains(QStringLiteral("control_bar.summary raster=%1 upload=%2 shown_ms=%3 shows=%4")));
}

// C5: the CPU bar is a child of the view itself (scrollContentsBy scrolls viewport children), opaque, and takes every
// mouse event.
TEST(FullscreenControlBarWiring, CpuBarIsAnOpaqueChildOfTheViewNotItsViewport)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(1, int(source.count(QStringLiteral("new FullscreenControlBarWidget( ui->graphicsView );"))));
    ASSERT_EQ(0, int(source.count(QStringLiteral("new FullscreenControlBarWidget( ui->graphicsView->viewport()"))));
    const QString widget = readRepoFile(QStringLiteral("platform/qt/FullscreenControlBar.cpp"));
    ASSERT_TRUE(widget.contains(QStringLiteral("setAttribute( Qt::WA_OpaquePaintEvent );")));
    ASSERT_TRUE(widget.contains(QStringLiteral("setAttribute( Qt::WA_NoSystemBackground );")));
    ASSERT_TRUE(widget.contains(QStringLiteral("setAttribute( Qt::WA_NoMousePropagation );")));
    ASSERT_TRUE(widget.contains(QStringLiteral("raise();")));
    ASSERT_TRUE(widget.contains(QStringLiteral("event->accept();")));
}

// C6: the functions open PR #834 (and #857) own carry no bar code.
TEST(FullscreenControlBarWiring, LookLabelAndToolbarOwnedFunctionsUntouched)
{
    const QString main = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const char *owned[] = {
        "void MainWindow::on_actionFullscreen_triggered( bool checked )",
        "void MainWindow::updateLookLabel( void )",
        "void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )",
        "void MainWindow::drawFrame( bool updateTimecodeLabel )",
        "int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)",
        "void MainWindow::updatePlaybackQualityIndicator( void )",
        "void MainWindow::on_actionPlay_toggled(bool checked)",
        "void MainWindow::recordPresentedFrame( const RenderFrameThread::ReadyFrame &readyFrame,",
        "void MainWindow::on_actionStop_triggered()",
    };
    for (const char *signature : owned)
    {
        const QString body = functionBodyToBrace(main, QString::fromLatin1(signature));
        ASSERT_FALSE(body.isEmpty());
        ASSERT_FALSE(mentionsBar(body));
    }
    const QString gpu = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_FALSE(mentionsBar(functionBodyToBrace(gpu, QStringLiteral("bool GpuDisplayWindow::setLookLabelIfActive(const QString &text)"))));
    ASSERT_FALSE(mentionsBar(functionBodyToBrace(gpu, QStringLiteral("void GpuDisplayWindow::drawLookLabel(int fbw, int fbh)"))));
    ASSERT_FALSE(mentionsBar(readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.cpp"))));
    ASSERT_FALSE(mentionsBar(readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.h"))));
    ASSERT_FALSE(mentionsBar(readRepoFile(QStringLiteral("platform/qt/LookLabelOverlay.cpp"))));
    ASSERT_FALSE(mentionsBar(readRepoFile(QStringLiteral("platform/qt/LookLabelOverlay.h"))));
}
