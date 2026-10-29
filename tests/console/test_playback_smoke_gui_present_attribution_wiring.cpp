// Wiring test (CPU-S2-WINDOWED-GUI-PRESENT-TELEMETRY-1): pins the GUI-thread present telemetry the
// CPU windowed-s2 attribution needs. On the CPU prep route the result waits longer for the GUI
// thread in windowed than in fullscreen (avg_playback_prep_result_queue_ms), but the receipts could
// not say which GUI-thread work costs it:
//   - presentPlaybackPreparedFrame set m_lastDrawFrameReadyImageMs = result.imageBuildMs (the prep
//     WORKER's build), so avg_draw_image_ms / avg_present_draw_present_ms duplicated
//     avg_playback_prep_worker_build_ms on every CPU leg and measured no GUI-thread work;
//   - the playback_prep_region_* spans were printed only on per-frame rows, never summarised;
//   - the preview viewport paint and the GUI thread's other work between presents were timed
//     nowhere (the playback_timeline_gui_busy_probe_* keys have no producer).
// MainWindow.cpp needs a full GUI build (not linked into console_tests), so this test reads the
// sources as text (mirrors test_playback_smoke_processed8_liveness_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
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
    const int next = nextSignature.isEmpty() ? source.length() : source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

// The balanced { ... } block that directly follows `anchor` (only whitespace between them);
// empty when the anchor is missing or is not followed by a block.
QString bracedBlockAfter(const QString & source, const QString & anchor)
{
    const int at = source.indexOf(anchor);
    if (at < 0) return QString();
    int open = at + anchor.length();
    while (open < source.length() && source.at(open).isSpace()) ++open;
    if (open >= source.length() || source.at(open) != QLatin1Char('{')) return QString();
    int depth = 0;
    for (int i = open; i < source.length(); ++i)
    {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open, i - open + 1);
    }
    return QString();
}

QString mainWindowSource()
{
    return readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
}

QString presentPreparedBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::presentPlaybackPreparedFrame( const PlaybackPrepResult &result )"),
                        QStringLiteral("void MainWindow::computeDisplaySceneGeometry( int sourceWidth,"));
}

QString prepResultReadyBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::onPlaybackPrepResultReady( void )"),
                        QStringLiteral("void MainWindow::openGuiPresentGap( void )"));
}

QString notePresentedFrameBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
                        QStringLiteral("void MainWindow::runPlaybackRoutePrewarm("));
}

QString finishSmokeBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
                        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
}

QString beginSmokeBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"),
                        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
}

QString closeGapBody()
{
    return bracedBlockAfter(mainWindowSource(), QStringLiteral("void MainWindow::closeGuiPresentGap( void )"));
}

// The literal text of the one qInfo() line whose format string starts with `kind`.
QString summaryFormat(const QString & finishBody, const QString & kind)
{
    const int at = finishBody.indexOf(kind);
    if (at < 0) return QString();
    const int end = finishBody.indexOf(QStringLiteral(".arg("), at);
    return end > at ? finishBody.mid(at, end - at) : QString();
}

const char * const kRegionSpans[] = { "setup", "image", "present", "finish", "total", "unattributed" };

} // namespace

TEST(GuiPresentAttributionWiring, DrawImageCostIsTheGuiImageRegionNotTheWorkerBuild)
{
    const QString body = presentPreparedBody();
    ASSERT_FALSE(body.isEmpty());
    // (a) the alias is gone at both sites.
    ASSERT_FALSE(body.contains(QStringLiteral("m_lastDrawFrameReadyImageMs = result.imageBuildMs")));
    ASSERT_FALSE(QRegularExpression(QStringLiteral(
        "m_lastDrawFrameReadyImageMs\\s*=\\s*result\\.imageBuildMs")).match(body).hasMatch());

    // The draw-image cost is the GUI-thread image region, assigned right after that region closes.
    const QString closeImage = QStringLiteral("prepRegionImageMs = prepRegionElapsedMs( prepRegionImageStartNs );");
    const int lastCloseAt = body.lastIndexOf(closeImage);
    ASSERT_TRUE(lastCloseAt >= 0);
    const int assignAt = body.indexOf(QStringLiteral("m_lastDrawFrameReadyImageMs = prepRegionImageMs;"));
    ASSERT_TRUE(assignAt > lastCloseAt);
    ASSERT_EQ(1, body.count(QStringLiteral("m_lastDrawFrameReadyImageMs = prepRegionImageMs;")));
    // The worker build keeps its own field.
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "QStringLiteral\\(\"playback_prep_worker_build_ms\"\\),\\s*result\\.imageBuildMs\\s*\\);"))
        .match(body).hasMatch());
}

TEST(GuiPresentAttributionWiring, RegionSpansAccumulateOnCpuRouteFramesOnly)
{
    const QString body = notePresentedFrameBody();
    ASSERT_FALSE(body.isEmpty());
    const QString gated = bracedBlockAfter(
        body, QStringLiteral("if( telemetryBoolValue( timing, \"playback_prep_cpu_route_active\" ) )"));
    ASSERT_FALSE(gated.isEmpty());
    for (const char * span : kRegionSpans)
    {
        const QString key = QStringLiteral("\"playback_prep_region_%1_ms\"").arg(QLatin1String(span));
        ASSERT_EQ(1, gated.count(QStringLiteral("telemetryDoubleValue( timing, %1 )").arg(key)));
    }
    ASSERT_TRUE(gated.contains(QStringLiteral("spans.regionSetupMs +=")));
    ASSERT_TRUE(gated.contains(QStringLiteral("spans.regionUnattributedMs +=")));
}

TEST(GuiPresentAttributionWiring, PrepSummaryCarriesTheSixRegionAverages)
{
    const QString body = finishSmokeBody();
    ASSERT_FALSE(body.isEmpty());
    const QString format = summaryFormat(body, QStringLiteral("playback_smoke.prep_summary session=%1"));
    ASSERT_FALSE(format.isEmpty());
    // (b) the six region keys, each once.
    for (const char * span : kRegionSpans)
    {
        const QString key = QStringLiteral("avg_playback_prep_region_%1_ms=").arg(QLatin1String(span));
        ASSERT_EQ(1, format.count(key));
    }
    // Averaged over CPU-route frames, not every presented frame.
    ASSERT_TRUE(body.contains(QStringLiteral("const auto avgCpuRouteMs = [&spans]( double sum ) -> double")));
    ASSERT_TRUE(body.contains(QStringLiteral(".arg( avgCpuRouteMs( spans.regionSetupMs ), 0, 'f', 3 )")));
    ASSERT_TRUE(body.contains(QStringLiteral(".arg( avgCpuRouteMs( spans.regionUnattributedMs ), 0, 'f', 3 )")));
}

TEST(GuiPresentAttributionWiring, GuiPresentSummaryLineCarriesPaintAndGuiBusyFields)
{
    const QString body = finishSmokeBody();
    ASSERT_FALSE(body.isEmpty());
    // (c) one gui_present_summary line with avg_ and max_ for the paint and the GUI busy figures.
    ASSERT_EQ(1, body.count(QStringLiteral("playback_smoke.gui_present_summary session=%1")));
    const QString format = summaryFormat(body, QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    ASSERT_FALSE(format.isEmpty());
    const QStringList keys = {
        QStringLiteral("preview_viewport_paints="),
        QStringLiteral("avg_preview_viewport_paint_ms="),
        QStringLiteral("max_preview_viewport_paint_ms="),
        QStringLiteral("gui_present_gaps="),
        QStringLiteral("avg_gui_present_gap_ms="),
        QStringLiteral("avg_gui_present_gap_idle_ms="),
        QStringLiteral("avg_gui_busy_ms="),
        QStringLiteral("max_gui_busy_ms="),
        QStringLiteral("avg_gui_other_busy_ms="),
        QStringLiteral("max_gui_other_busy_ms="),
    };
    for (const QString & key : keys)
    {
        ASSERT_EQ(1, format.count(key));
    }
}

TEST(GuiPresentAttributionWiring, PreviewViewportPaintIsTimedAroundTheBasePaint)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void paintEvent(QPaintEvent *event) override;")));
    ASSERT_TRUE(header.contains(QStringLiteral("void resetPaintTiming(void);")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.cpp"));
    const QString paint = bracedBlockAfter(source, QStringLiteral("void GraphicsZoomView::paintEvent(QPaintEvent *event)"));
    ASSERT_FALSE(paint.isEmpty());
    const int scopeAt = paint.indexOf(QStringLiteral("const preview_paint_timing::Scope timing;"));
    const int baseAt = paint.indexOf(QStringLiteral("QGraphicsView::paintEvent(event);"));
    ASSERT_TRUE(scopeAt >= 0);
    ASSERT_TRUE(baseAt > scopeAt);
    ASSERT_EQ(1, paint.count(QStringLiteral("QGraphicsView::paintEvent(event);")));

    // The view reads and resets the one shared sink, not a count of its own paints only.
    ASSERT_TRUE(bracedBlockAfter(source, QStringLiteral("GraphicsZoomView::PaintTiming GraphicsZoomView::paintTiming() const"))
                    .contains(QStringLiteral("return preview_paint_timing::totals();")));
    ASSERT_TRUE(bracedBlockAfter(source, QStringLiteral("void GraphicsZoomView::resetPaintTiming()"))
                    .contains(QStringLiteral("preview_paint_timing::reset();")));

    // The session resets it and the summary reads it.
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("ui->graphicsView->resetPaintTiming();")));
    ASSERT_TRUE(finishSmokeBody().contains(QStringLiteral("ui->graphicsView->paintTiming()")));
}

TEST(GuiPresentAttributionWiring, GuiBusyGapOpensAfterPresentAndClosesOnTheNextResult)
{
    const QString ready = prepResultReadyBody();
    ASSERT_FALSE(ready.isEmpty());
    // Closed only once a result was taken (an empty-queue wakeup is not the next result).
    const int emptyAt = ready.indexOf(QStringLiteral("if( m_playbackPrepResults.empty() ) return;"));
    const int closeAt = ready.indexOf(QStringLiteral("closeGuiPresentGap();"));
    const int presentAt = ready.indexOf(QStringLiteral("presentPlaybackPreparedFrame( result );"));
    const int openAt = ready.indexOf(QStringLiteral("openGuiPresentGap();"));
    ASSERT_TRUE(emptyAt >= 0);
    ASSERT_TRUE(closeAt > emptyAt);
    ASSERT_TRUE(presentAt > closeAt);
    ASSERT_TRUE(openAt > presentAt);

    // Idle is the event loop blocked in its wait, from the GUI dispatcher's own signals, and
    // the connection exists only under the smoke-telemetry env gate (zero cost when off).
    const QString source = mainWindowSource();
    const QString gate = bracedBlockAfter(source, QStringLiteral("if( playbackSmokeFrameTelemetryEnabled() )"));
    ASSERT_FALSE(gate.isEmpty());
    ASSERT_TRUE(gate.contains(QStringLiteral("&QAbstractEventDispatcher::aboutToBlock")));
    ASSERT_TRUE(gate.contains(QStringLiteral("&QAbstractEventDispatcher::awake")));
}

TEST(GuiPresentAttributionWiring, SmokeRunnerSleepIsReportedApartFromGuiBusy)
{
    // Adversary A1 (400865b1): the measured gui-smoke loop is processEvents(AllEvents) then
    // QThread::msleep( 10 ). That sleep never reaches the dispatcher's aboutToBlock, so it was
    // charged as GUI busy. Every runner sleep goes through the accounting helper, the gap keeps
    // it apart, and busy excludes it.
    const QString source = mainWindowSource();
    const QString runners[][2] = {
        { QStringLiteral("int MainWindow::runHeadlessPlaybackProfile(const PlaybackProfileOptions & options)"),
          QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)") },
        { QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
          QStringLiteral("bool MainWindow::importNewMlv(QString fileName)") },
    };
    for (const auto & runner : runners)
    {
        const QString body = functionBody(source, runner[0], runner[1]);
        ASSERT_FALSE(body.isEmpty());
        ASSERT_EQ(0, body.count(QStringLiteral("QThread::msleep(")));
        ASSERT_TRUE(body.count(QStringLiteral("sleepGuiThreadForSmokeRunner(")) > 0);
    }

    const QString helper = bracedBlockAfter(
        source, QStringLiteral("void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )"));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_EQ(1, helper.count(QStringLiteral("QThread::msleep( ms );")));
    ASSERT_TRUE(helper.contains(QStringLiteral("m_guiPresentGapRunnerSleepNs +=")));

    const QString close = closeGapBody();
    ASSERT_FALSE(close.isEmpty());
    ASSERT_TRUE(close.contains(QStringLiteral("const double busyMs = gapMs - idleMs - runnerSleepMs;")));
    ASSERT_TRUE(close.contains(QStringLiteral("gaps.runnerSleepSumMs += runnerSleepMs;")));

    const QString format = summaryFormat(finishSmokeBody(),
                                         QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    ASSERT_EQ(1, format.count(QStringLiteral("avg_gui_present_gap_runner_sleep_ms=")));
}

TEST(GuiPresentAttributionWiring, PendingDispatcherWaitIsIdleWhenTheResultArrivesFirst)
{
    // Adversary A2 (400865b1): a dispatcher that delivers the next result before it emits awake
    // left the wait pending; the close dropped it and charged the whole wait as busy. The close
    // now counts a pending wait as idle up to the close, before it derives idle.
    const QString close = closeGapBody();
    ASSERT_FALSE(close.isEmpty());
    const QRegularExpressionMatch pending = QRegularExpression(QStringLiteral(
        "if\\( m_guiPresentGapBlockStartNs >= 0 \\)\\s*"
        "m_guiPresentGapIdleNs \\+= nowNs - m_guiPresentGapBlockStartNs;")).match(close);
    ASSERT_TRUE(pending.hasMatch());
    const int idleAt = close.indexOf(QStringLiteral("const double idleMs ="));
    ASSERT_TRUE(idleAt > pending.capturedStart());
    ASSERT_TRUE(close.indexOf(QStringLiteral("const qint64 nowNs = m_guiPresentGapClock.nsecsElapsed();"))
                < pending.capturedStart());
}

TEST(GuiPresentAttributionWiring, GlViewportAndGlWindowPaintsReachTheSamePaintSink)
{
    // Adversary A1 (3614eb25): with the GL viewport installed, its event filter consumes Paint
    // while a GPU frame is pending, so GraphicsZoomView::paintEvent never ran and that paint was
    // charged to other busy; the GL window paints the preview outside the view entirely. Both
    // time their paint into the shared sink.
    const QString viewport = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    ASSERT_TRUE(viewport.contains(QStringLiteral("#include \"PreviewPaintTiming.h\"")));
    const QString consumed = bracedBlockAfter(
        viewport,
        QStringLiteral("if ( watched == this && event->type() == QEvent::Paint && hasPendingFrame() )"));
    ASSERT_FALSE(consumed.isEmpty());
    const int scopeAt = consumed.indexOf(QStringLiteral("const preview_paint_timing::Scope paintTiming;"));
    const int eventAt = consumed.indexOf(QStringLiteral("QOpenGLWidget::event(event);"));
    ASSERT_TRUE(scopeAt >= 0);
    ASSERT_TRUE(eventAt > scopeAt);

    const QString window = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(window.contains(QStringLiteral("#include \"PreviewPaintTiming.h\"")));
    const QString paintGl = bracedBlockAfter(window, QStringLiteral("void GpuDisplayWindow::paintGL()"));
    ASSERT_FALSE(paintGl.isEmpty());
    const int windowScopeAt = paintGl.indexOf(QStringLiteral("const preview_paint_timing::Scope paintTiming;"));
    ASSERT_TRUE(windowScopeAt >= 0);
    ASSERT_TRUE(windowScopeAt < paintGl.indexOf(QStringLiteral("glClear(")));

    // The sink is header-only: gui_tests links GpuDisplayViewport/Window without GraphicsZoomView.
    const QString sink = readRepoFile(QStringLiteral("platform/qt/PreviewPaintTiming.h"));
    ASSERT_TRUE(sink.contains(QStringLiteral("inline Totals &totals()")));
    ASSERT_TRUE(sink.contains(QStringLiteral("~Scope()")));
}
