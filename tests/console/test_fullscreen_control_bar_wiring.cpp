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
    // Adversary r1 pass 4 A2: every non-wheel mouse event in the application (any watched object, either path)
    // settles the bar's stale ownership, before the hidden-bar early return and the GL-only test.
    const int settleAt = gpu.indexOf(QStringLiteral("fullscreen_bar::settle( &m_fullscreenBarInput, event->type(), mouse->button(), mouse->buttons() );"));
    ASSERT_TRUE(settleAt > gpu.indexOf(QStringLiteral("if( event->type() != QEvent::Wheel )")));
    ASSERT_TRUE(settleAt > gpu.indexOf(QStringLiteral("default:\n        return false;")));
    ASSERT_TRUE(settleAt < gpu.indexOf(QStringLiteral("if( !fullscreenBarShown() && m_fullscreenBarInput.owned == Qt::NoButton")));
    ASSERT_TRUE(settleAt < gpu.indexOf(QStringLiteral("if( !gl || watched != gl ) return false;")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/FullscreenControlBarPolicy.h"));
    const QString routeBody = functionBodyToBrace(policy, QStringLiteral("inline bool route( InputState *s,"));
    ASSERT_TRUE(routeBody.contains(QStringLiteral("settle( s, type, button, buttons );")));

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
    // Adversary r1 pass 2 A1: the press a QWindow gets before the double-click it created is taken but does not act.
    ASSERT_TRUE(input.contains(QStringLiteral("pressCreatedDoubleClick = mouse->flags().testFlag( Qt::MouseEventCreatedDoubleClick );")));
    const int routeCall = input.indexOf(QStringLiteral("fullscreen_bar::route( &m_fullscreenBarInput"));
    const int actionOfCall = input.indexOf(QStringLiteral("action = fullscreen_bar::actionOf( action, event->type(), pressCreatedDoubleClick );"));
    ASSERT_TRUE(actionOfCall > routeCall);
    ASSERT_TRUE(actionOfCall < input.indexOf(QStringLiteral("switch( action )")));
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
    // Before the capture readback (the last test of the flag; the first is the no-frame branch).
    ASSERT_TRUE(paint.lastIndexOf(QStringLiteral("drawControlBar(fbw, fbh);")) < paint.lastIndexOf(QStringLiteral("if ( m_captureReadbackRequested )\n")));
    // Adversary r1 pass 1 A4: the no-frame branch (no clip yet, or a refused recon texture) returns before the frame
    // draw, yet MainWindow still routes the bar's clicks there, so that branch draws the bar too: the controls a click
    // can hit are always the controls on screen. Exactly two calls: the no-frame branch and after the look label.
    ASSERT_EQ(2, int(paint.count(QStringLiteral("drawControlBar(fbw, fbh);"))));
    const int noFrameAt = paint.indexOf(QStringLiteral("if ( !m_texture || !activeProgram || width() <= 0 || height() <= 0 || reconRefused )"));
    ASSERT_TRUE(noFrameAt > 0);
    const int noFrameReturn = paint.indexOf(QStringLiteral("return;"), noFrameAt);
    const int earlyBar = paint.indexOf(QStringLiteral("drawControlBar(fbw, fbh);"), noFrameAt);
    ASSERT_TRUE(earlyBar > noFrameAt);
    ASSERT_TRUE(earlyBar < noFrameReturn);

    const QString draw = functionBodyToBrace(source, QStringLiteral("void GpuDisplayWindow::drawControlBar(int fbw, int fbh)"));
    ASSERT_FALSE(draw.isEmpty());
    ASSERT_TRUE(firstStatement(draw) == QStringLiteral("if ( !m_controlBar.shown() ) return;"));
    // Adversary r1 pass 3 A1: a change of the bar's SHOWN state asks for one paint even while playing (a hide must not
    // wait for the next frame, and a show and hide between two paints must be seen); a feed-only change (position,
    // timecode) while playing asks for none: the next frame's paint draws it.
    const QString state = functionBodyToBrace(source,
        QStringLiteral("bool GpuDisplayWindow::setControlBarStateIfActive(bool shown, const fullscreen_bar::Feed &feed, qint64 nowMs)"));
    ASSERT_FALSE(state.isEmpty());
    ASSERT_EQ(1, int(state.count(QStringLiteral("update()"))));
    ASSERT_EQ(1, int(state.count(QStringLiteral("if ( changedShown || ( changedFeed && !feed.playing ) ) win->update();"))));
    ASSERT_TRUE(state.contains(QStringLiteral("const bool changedShown = shown ? win->m_controlBar.show(nowMs) : win->m_controlBar.hide(nowMs);")));
    ASSERT_TRUE(state.contains(QStringLiteral("const bool changedFeed = win->m_controlBar.noteFeed(feed);")));

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
    // Adversary r1 pass 1 A5: a move the pointer witness saw while the cursor was already shown (an input-excluded
    // wait deferred its event) is a Rearm tick, not Show: it still shows a hidden bar. Pass 2 A4: the witness is real
    // movement already (seeded at entry; synthesized moves told apart), so it is not re-gated on the cursor's position
    // now, which an away-and-back has returned to the entry point.
    const int rearmAt = timeout.indexOf(QStringLiteral("case viewer_idle::Tick::Rearm:"));
    ASSERT_TRUE(rearmAt > 0);
    const QString rearm = timeout.mid(rearmAt, timeout.indexOf(QStringLiteral("break;"), rearmAt) - rearmAt);
    ASSERT_TRUE(rearm.contains(QStringLiteral("if( in.pointerMoved && !fullscreenBarShown() ) viewerIdleApplyShown( true, true );")));
    ASSERT_FALSE(rearm.contains(QStringLiteral("QCursor::pos()")));
    // Adversary r1 pass 4 A3: a witnessed move counts as having left the entry position on every tick, whether or not
    // the bar is shown (set before the tick is decided), so a later return to the entry position shows a hidden bar.
    const int sawAt = timeout.indexOf(QStringLiteral("if( in.pointerMoved ) m_fullscreenBarSawMove = true;"));
    ASSERT_TRUE(sawAt > timeout.indexOf(QStringLiteral("in.pointerMoved = m_viewerIdlePointer.moved( QCursor::pos() );")));
    ASSERT_TRUE(sawAt < timeout.indexOf(QStringLiteral("switch( m_viewerIdle.onTimeout(")));
    ASSERT_EQ(1, int(source.count(QStringLiteral("m_fullscreenBarSawMove = true;"))));
    // A delivered move is judged at its own position, not the cursor's now (a deferred move delivered after the
    // pointer came back), and on every activity, also while the bar is shown (pass 4 A3): only the show is gated.
    const QString note = functionBodyToBrace(source, QStringLiteral("void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )"));
    ASSERT_TRUE(note.contains(QStringLiteral("m_fullscreenBarEntryPos, m_viewerIdleLastGlobal.toPoint() );")));
    ASSERT_TRUE(note.indexOf(QStringLiteral("viewer_idle::eventIsActivity( event, &m_viewerIdleLastGlobal")) < note.indexOf(QStringLiteral("m_viewerIdleLastGlobal.toPoint()")));
    const int judgedAt = note.indexOf(QStringLiteral("const bool showsBar = fullscreen_bar::activityShowsBar( event->type(), &m_fullscreenBarSawMove,"));
    ASSERT_TRUE(judgedAt > 0);
    ASSERT_TRUE(judgedAt < note.indexOf(QStringLiteral("if( wasHidden || !fullscreenBarShown() ) viewerIdleApplyShown( true, showsBar );")));
    ASSERT_EQ(1, int(source.count(QStringLiteral("fullscreen_bar::activityShowsBar("))));
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

    // Adversary r1 pass 1 A2: the raster cache is keyed on the bar's device-pixel size, which a DPR change can leave
    // equal (1920x1080 at 1.0 -> 960x540 at 2.0). A pixmap cached at another ratio is dropped before the raster
    // decision, so what is drawn always matches the current-ratio layout the clicks are hit-tested against.
    const int paintAt = widget.indexOf(QStringLiteral("void FullscreenControlBarWidget::paintEvent( QPaintEvent *event )"));
    ASSERT_TRUE(paintAt > 0);
    const QString paint = widget.mid(paintAt);
    const int dropAt = paint.indexOf(QStringLiteral(
        "if( !m_pixmap.isNull() && !qFuzzyCompare( m_pixmap.devicePixelRatio(), dpr ) ) m_presenter.dropRaster();"));
    ASSERT_TRUE(dropAt > 0);
    ASSERT_TRUE(dropAt < paint.indexOf(QStringLiteral("m_presenter.rasterFor( l.bar.size() );")));

    // Adversary r1 pass 3 A2: a ratio or screen change the view saw no Resize for re-follows the view, and a position
    // on the widget maps to the viewer through the painted layout (pixmap at the widget's origin), never through pos().
    const QString changes = functionBodyToBrace(widget, QStringLiteral("bool viewGeometryChanges( QEvent::Type type )"));
    ASSERT_FALSE(changes.isEmpty());
    ASSERT_TRUE(changes.contains(QStringLiteral("case QEvent::Resize:")));
    ASSERT_TRUE(changes.contains(QStringLiteral("case QEvent::ScreenChangeInternal:")));
    ASSERT_TRUE(changes.contains(QStringLiteral(
        "#if QT_VERSION >= QT_VERSION_CHECK(6, 6, 0)\n    case QEvent::DevicePixelRatioChange:\n#endif")));
    const QString filter = functionBodyToBrace(widget, QStringLiteral("bool FullscreenControlBarWidget::eventFilter( QObject *watched, QEvent *event )"));
    ASSERT_TRUE(filter.contains(QStringLiteral("if( watched == m_view && m_presenter.shown() && viewGeometryChanges( event->type() ) )")));
    ASSERT_TRUE(filter.contains(QStringLiteral("followView();")));
    const QString route = functionBodyToBrace(widget, QStringLiteral("bool FullscreenControlBarWidget::route( QEvent *event, const QPointF &localPos )"));
    ASSERT_TRUE(route.contains(QStringLiteral("return m_input( l, event, QPointF( l.bar.topLeft() ) + localPos * dpr );")));
    ASSERT_FALSE(route.contains(QStringLiteral("pos()")));

    // Adversary r1 pass 3 A3, CPU sibling: a wheel the bar does not take (a gesture begun elsewhere is held) goes back
    // to the viewport; MainWindow decides it with the GL path's claim.
    const QString wheel = functionBodyToBrace(widget, QStringLiteral("void FullscreenControlBarWidget::wheelEvent( QWheelEvent *event )"));
    ASSERT_TRUE(wheel.contains(QStringLiteral("if( !route( event, localPosOf( event ) ) ) forwardWheelToView( event );")));
    const QString forward = functionBodyToBrace(widget, QStringLiteral("void FullscreenControlBarWidget::forwardWheelToView( QWheelEvent *event )"));
    ASSERT_TRUE(forward.contains(QStringLiteral("target = area->viewport();")));
    ASSERT_TRUE(forward.contains(QStringLiteral("QCoreApplication::sendEvent( target, &copy );")));
    ASSERT_EQ(1, int(source.count(QStringLiteral("{ return fullscreenBarInput( layout, event, devicePos, true ); } );"))));
    const QString input = functionBodyToBrace(source,
        QStringLiteral("bool MainWindow::fullscreenBarInput( const fullscreen_bar::Layout &layout, QEvent *event, const QPointF &devicePos, bool fromWidget )"));
    const int gateAt = input.indexOf(QStringLiteral(
        "&& !fullscreen_bar::claims( m_fullscreenBarInput, fullscreenBarShown(), event->type(), button, buttons, part ) ) return false;"));
    const int wheelIfAt = input.indexOf(QStringLiteral("if( fromWidget && event->type() == QEvent::Wheel"));
    ASSERT_TRUE(wheelIfAt > 0);
    ASSERT_TRUE(gateAt > wheelIfAt);
    ASSERT_TRUE(gateAt < input.indexOf(QStringLiteral("if( fromWidget ) action = fullscreen_bar::onMouse(")));
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
