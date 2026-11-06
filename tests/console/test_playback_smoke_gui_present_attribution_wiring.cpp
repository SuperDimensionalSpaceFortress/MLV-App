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
#include "../../platform/qt/GuiPresentGapIdle.h"
#include "../../platform/qt/PreviewPaintTiming.h"
#include "../../platform/qt/ReconWorkerSplit.h"

#include <QJsonObject>

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

const qint64 kNsPerMs = 1000000;

const char * const kRegionSpans[] ={ "setup", "image", "present", "finish", "total", "unattributed" };

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
    // the connection exists only under the smoke-telemetry env gate (zero cost when off). r2: it
    // sits in its own block, outside the pinned applicationStateChanged block
    // (PlaybackSmokeForegroundWiring pins that block byte for byte).
    const QString source = mainWindowSource();
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "QAbstractEventDispatcher \\*const guiPresentIdleDispatcher = playbackSmokeFrameTelemetryEnabled\\(\\)\\s*"
        "\\? QAbstractEventDispatcher::instance\\( thread\\(\\) \\) : nullptr;")).match(source).hasMatch());
    const QString gate = bracedBlockAfter(source, QStringLiteral("if( guiPresentIdleDispatcher )"));
    ASSERT_FALSE(gate.isEmpty());
    ASSERT_TRUE(gate.contains(QStringLiteral("&QAbstractEventDispatcher::aboutToBlock")));
    ASSERT_TRUE(gate.contains(QStringLiteral("&QAbstractEventDispatcher::awake")));
    ASSERT_TRUE(gate.contains(QStringLiteral("m_guiPresentIdleProbeConnected = true;")));
}

TEST(GuiPresentAttributionWiring, SmokeRunnerSleepIsReportedApartFromGuiBusy)
{
    // Adversary A1 (400865b1): the measured gui-smoke loop is processEvents(AllEvents) then
    // QThread::msleep( 10 ). That sleep never reaches the dispatcher's aboutToBlock, so it was
    // charged as GUI busy. Every runner sleep goes through the accounting helper, the gap keeps
    // it apart, and busy excludes it. The two Play-poll loops of runHeadlessPlaybackProfile are
    // pinned ENFORCE-3 stop statements (tools/repo_hygiene/app_play_scan.py PINNED_STOP_STATEMENTS);
    // PLAYBACK-PRESENT-TAIL-TIMING-1 r4 (ADV-A1): their sleep goes through the helper too, because a
    // GPU window's present-tail window is open across it. No raw sleep is left in either runner.
    const QString source = mainWindowSource();
    const QString smoke = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smoke.isEmpty());
    ASSERT_EQ(0, smoke.count(QStringLiteral("QThread::msleep(")));
    ASSERT_TRUE(smoke.count(QStringLiteral("sleepGuiThreadForSmokeRunner(")) > 0);

    const QString profile = functionBody(source,
        QStringLiteral("int MainWindow::runHeadlessPlaybackProfile(const PlaybackProfileOptions & options)"),
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"));
    ASSERT_FALSE(profile.isEmpty());
    ASSERT_TRUE(profile.count(QStringLiteral("sleepGuiThreadForSmokeRunner(")) > 0);
    ASSERT_EQ(0, profile.count(QStringLiteral("QThread::msleep(")));
    const QRegularExpression pinnedPoll(QStringLiteral(
        "while\\( \\( (autoSettleState|playActionState) = programmaticPlayState\\([^\\n]*\\)\\s*"
        "== playback_frame_range::PlayStopState::Continue \\)\\s*\\{\\s*"
        "qApp->processEvents\\( QEventLoop::AllEvents \\);\\s*sleepGuiThreadForSmokeRunner\\( 10 \\);\\s*\\}"));
    int pinnedPolls = 0;
    for (QRegularExpressionMatchIterator it = pinnedPoll.globalMatch(profile); it.hasNext(); it.next())
        ++pinnedPolls;
    ASSERT_EQ(2, pinnedPolls);

    const QString helper = bracedBlockAfter(
        source, QStringLiteral("void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )"));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_EQ(1, helper.count(QStringLiteral("QThread::msleep( ms );")));
    ASSERT_TRUE(helper.contains(QStringLiteral("m_guiPresentGapRunnerSleepNs +=")));

    const QString close = closeGapBody();
    ASSERT_FALSE(close.isEmpty());
    ASSERT_TRUE(close.contains(QStringLiteral("m_guiPresentGapIdle.idleNs, m_guiPresentGapRunnerSleepNs,")));
    ASSERT_TRUE(close.contains(QStringLiteral("gaps.runnerSleepSumMs += split.runnerSleepMs;")));
    // gap 20, idle 8, runner sleep 2, paint 3 -> busy 10, other busy 7.
    const gui_present_gap::Split split = gui_present_gap::split(20 * kNsPerMs, 8 * kNsPerMs, 2 * kNsPerMs, 3.0);
    ASSERT_NEAR(2.0, split.runnerSleepMs, 1e-9);
    ASSERT_NEAR(10.0, split.busyMs, 1e-9);
    ASSERT_NEAR(7.0, split.otherBusyMs, 1e-9);

    const QString format = summaryFormat(finishSmokeBody(),
                                         QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    ASSERT_EQ(1, format.count(QStringLiteral("avg_gui_present_gap_runner_sleep_ms=")));
}

TEST(GuiPresentAttributionWiring, GuiIdleLateAwakeCallbackWorkStaysBusy)
{
    // GUI-IDLE-LATE-AWAKE (sol r2 B1, ADV-A1): QEventDispatcherGlib emits awake only after
    // g_main_context_iteration() dispatched the ready callbacks, so a wait ended at awake (or, when
    // the result came first, at the close) counted that callback work as idle. A wait now ends at
    // the first event the GUI thread dispatches after it, or at awake, whichever comes first.
    // Input 1: block 0; wait 10; work 5 (first dispatch at 10); awake 15; next block 15; the
    // result is dispatched at 30 before awake and closes the gap at 30.
    gui_present_gap::IdleWaits waits;
    waits.blocked(0);
    waits.ended(10 * kNsPerMs);
    waits.ended(15 * kNsPerMs);
    waits.blocked(15 * kNsPerMs);
    waits.ended(30 * kNsPerMs);
    gui_present_gap::Split split = gui_present_gap::split(30 * kNsPerMs, waits.idleNs, 0, 0.0);
    ASSERT_NEAR(25.0, split.idleMs, 1e-9);
    ASSERT_NEAR(5.0, split.busyMs, 1e-9);
    ASSERT_NEAR(5.0, split.otherBusyMs, 1e-9);
    // Input 2: block 0; wait 10; callback work 4 (dispatched at 10); close at 14 before awake.
    waits = gui_present_gap::IdleWaits();
    waits.blocked(0);
    waits.ended(10 * kNsPerMs);
    split = gui_present_gap::split(14 * kNsPerMs, waits.idleNs, 0, 0.0);
    ASSERT_NEAR(10.0, split.idleMs, 1e-9);
    ASSERT_NEAR(4.0, split.busyMs, 1e-9);

    // Wiring: awake and the application event filter both end a wait, the filter before anything
    // else it does; the close never charges a still-pending wait as idle.
    const QString source = mainWindowSource();
    const QString gate = bracedBlockAfter(source, QStringLiteral("if( guiPresentIdleDispatcher )"));
    ASSERT_TRUE(gate.contains(QStringLiteral("m_guiPresentGapIdle.blocked( m_guiPresentGapClock.nsecsElapsed() );")));
    ASSERT_TRUE(QRegularExpression(QStringLiteral(
        "&QAbstractEventDispatcher::awake,\\s*this, &MainWindow::endGuiPresentIdleWait \\);")).match(gate).hasMatch());
    ASSERT_TRUE(source.contains(QStringLiteral("qApp->installEventFilter( this );")));
    const QString filter = bracedBlockAfter(source, QStringLiteral("bool MainWindow::eventFilter(QObject *watched, QEvent *event)"));
    const int endAt = filter.indexOf(QStringLiteral("endGuiPresentIdleWait();"));
    ASSERT_TRUE(endAt >= 0);
    ASSERT_TRUE(endAt < filter.indexOf(QStringLiteral("event->type()")));
    const QString endWait = bracedBlockAfter(source, QStringLiteral("void MainWindow::endGuiPresentIdleWait( void )"));
    ASSERT_TRUE(endWait.contains(QStringLiteral("m_guiPresentGapIdle.ended( m_guiPresentGapClock.nsecsElapsed() );")));
    const QString close = closeGapBody();
    ASSERT_FALSE(close.isEmpty());
    ASSERT_FALSE(close.contains(QStringLiteral(".ended(")));
    ASSERT_FALSE(close.contains(QStringLiteral("BlockStartNs")));
}

TEST(GuiPresentAttributionWiring, GuiPaintDirectGrabReachesThePaintSinkOnce)
{
    // GUI-PAINT-DIRECT-GRAB (sol r2 B2, r1 OPEN A2): QOpenGLWidget::grabFramebuffer() calls
    // paintGL directly, with no Paint event, so the GL viewport's event-filter Scope never saw it
    // and the paint was charged to other busy. paintGL now opens its own Scope; only the outermost
    // Scope counts, so a Paint-event paint (Scope around QOpenGLWidget::event -> paintGL) is still
    // counted once.
    preview_paint_timing::reset();
    const auto paintGl = []() { const preview_paint_timing::Scope paintTiming; };
    paintGl(); // grabFramebuffer() outside a paint
    {
        const preview_paint_timing::Scope paintEvent; // the event filter's consumed Paint
        paintGl();
    }
    ASSERT_EQ(2u, static_cast<unsigned>(preview_paint_timing::totals().paints));
    ASSERT_EQ(0, preview_paint_timing::scopeDepth());
    preview_paint_timing::reset();

    const QString viewport = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    const QString paintGlBody = bracedBlockAfter(viewport, QStringLiteral("void GpuDisplayViewport::paintGL()"));
    const int scopeAt = paintGlBody.indexOf(QStringLiteral("const preview_paint_timing::Scope paintTiming;"));
    ASSERT_TRUE(scopeAt >= 0);
    ASSERT_TRUE(scopeAt < paintGlBody.indexOf(QStringLiteral("m_paintedSerialValid = false;")));
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

// ---- CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 -------------------------------------------------------------

namespace
{

QString renderFrameThreadSource()
{
    return readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
}

gui_present_gap::SpanClock spanClock(quint64 serial, qint64 nowMs, qint64 idleMs = 0, qint64 sleepMs = 0, double paintMs = 0.0)
{
    gui_present_gap::SpanClock c;
    c.gapSerial = serial;
    c.nowNs = nowMs * kNsPerMs;
    c.idleNs = idleMs * kNsPerMs;
    c.runnerSleepNs = sleepMs * kNsPerMs;
    c.paintMs = paintMs;
    return c;
}

} // namespace

TEST(ReconWorkerSplit, CopiesOnlyTheWorkersSplitKeysUnderTheirOwnPrefix)
{
    // Input: the slot as the recon worker left it (its own llrawproc / full20 values) plus keys this
    // split must not carry. The render thread's own (zero) values sit under the unprefixed keys.
    QJsonObject worker;
    worker.insert(QStringLiteral("llrawproc_total_ms"), 30.0);
    worker.insert(QStringLiteral("llrawproc_dual_iso_ms"), 26.0);
    worker.insert(QStringLiteral("dual_iso_full20_total_ms"), 25.0);
    worker.insert(QStringLiteral("dual_iso_full20_mix_chroma_ms"), 7.0);
    worker.insert(QStringLiteral("dual_iso_full20_valid"), true);
    worker.insert(QStringLiteral("dual_iso_full20_threads"), 12);           // not a duration
    worker.insert(QStringLiteral("render_thread_recon_worker_llrawproc_total_ms"), 30.0);
    worker.insert(QStringLiteral("cpu_dualiso_recon_scale"), 1);
    QJsonObject target;
    target.insert(QStringLiteral("llrawproc_total_ms"), 0.0);
    target.insert(QStringLiteral("dual_iso_full20_total_ms"), 0.0);
    recon_worker_split::copyWorkerSplit(worker, target);
    ASSERT_NEAR(30.0, target.value(QStringLiteral("recon_worker_llrawproc_total_ms")).toDouble(), 1e-9);
    ASSERT_NEAR(26.0, target.value(QStringLiteral("recon_worker_llrawproc_dual_iso_ms")).toDouble(), 1e-9);
    ASSERT_NEAR(7.0, target.value(QStringLiteral("recon_worker_dual_iso_full20_mix_chroma_ms")).toDouble(), 1e-9);
    ASSERT_TRUE(target.value(QStringLiteral("recon_worker_dual_iso_full20_valid")).toBool());
    ASSERT_FALSE(target.contains(QStringLiteral("recon_worker_dual_iso_full20_threads")));
    ASSERT_FALSE(target.contains(QStringLiteral("recon_worker_render_thread_recon_worker_llrawproc_total_ms")));
    ASSERT_FALSE(target.contains(QStringLiteral("recon_worker_cpu_dualiso_recon_scale")));
    // The render thread's own keys are never rewritten.
    ASSERT_NEAR(0.0, target.value(QStringLiteral("llrawproc_total_ms")).toDouble(), 1e-9);
    ASSERT_NEAR(0.0, target.value(QStringLiteral("dual_iso_full20_total_ms")).toDouble(), 1e-9);
}

TEST(ReconWorkerSplit, AveragesAndResiduesAtEachLevelOfTheNesting)
{
    // Two presented frames with the split, one without (not counted). Frame values:
    // llrawproc 40 = dark 1 + vstripes 1 + focus 1 + bad 1 + pattern_noise 1 + dual_iso 30 + chroma_smooth 1 (+4 residue);
    // dual_iso 30 > full20 28 (+2 outside); full20 28 = pattern 1 noise 1 scratch 1 convert20 1 match 1 interp 5 fullres 2
    // mix 10 final_blend 4 convert16 1 (+1 residue); mix 10 = halfres 2 chroma 6 alias 1 overexposed 0 (+1); chroma 6 = copy 1
    // fullres 2 halfres 2 (+1).
    QJsonObject frame;
    const auto put = [&frame](const char *key, double v) {
        frame.insert(recon_worker_split::prefix() + QLatin1String(key), v);
    };
    put("llrawproc_total_ms", 40); put("llrawproc_dark_frame_ms", 1); put("llrawproc_vertical_stripes_ms", 1);
    put("llrawproc_focus_pixels_ms", 1); put("llrawproc_bad_pixels_ms", 1); put("llrawproc_pattern_noise_ms", 1);
    put("llrawproc_dual_iso_ms", 30); put("llrawproc_chroma_smooth_ms", 1); put("llrawproc_other_ms", 4);
    put("dual_iso_full20_total_ms", 28); put("dual_iso_full20_pattern_ms", 1); put("dual_iso_full20_noise_ms", 1);
    put("dual_iso_full20_scratch_ms", 1); put("dual_iso_full20_convert20_ms", 1); put("dual_iso_full20_match_ms", 1);
    put("dual_iso_full20_interp_ms", 5); put("dual_iso_full20_fullres_ms", 2); put("dual_iso_full20_mix_ms", 10);
    put("dual_iso_full20_mix_halfres_ms", 2); put("dual_iso_full20_mix_chroma_ms", 6); put("dual_iso_full20_mix_chroma_copy_ms", 1);
    put("dual_iso_full20_mix_chroma_fullres_ms", 2); put("dual_iso_full20_mix_chroma_halfres_ms", 2);
    put("dual_iso_full20_mix_alias_map_ms", 1); put("dual_iso_full20_mix_overexposed_ms", 0);
    put("dual_iso_full20_final_blend_ms", 4); put("dual_iso_full20_convert16_ms", 1); put("dual_iso_full20_other_ms", 1);
    frame.insert(recon_worker_split::prefix() + QStringLiteral("dual_iso_full20_valid"), true);

    recon_worker_split::Totals totals;
    ASSERT_TRUE(totals.add(frame));
    ASSERT_TRUE(totals.add(frame));
    QJsonObject ownRecon;   // a frame whose render ran its own recon: the render thread's keys only
    ownRecon.insert(QStringLiteral("llrawproc_total_ms"), 33.0);
    ASSERT_FALSE(totals.add(ownRecon));
    ASSERT_EQ(2u, static_cast<unsigned>(totals.frames));
    ASSERT_EQ(2u, static_cast<unsigned>(totals.full20ValidFrames));

    const auto avg = totals.averages();
    ASSERT_NEAR(40.0, avg[recon_worker_split::LlrawprocTotal], 1e-9);
    ASSERT_NEAR(6.0, avg[recon_worker_split::Full20MixChroma], 1e-9);
    const recon_worker_split::Unattributed u = recon_worker_split::unattributed(avg);
    ASSERT_NEAR(4.0, u.llrawprocMs, 1e-9);
    ASSERT_NEAR(2.0, u.dualIsoOutsideFull20Ms, 1e-9);
    ASSERT_NEAR(1.0, u.full20Ms, 1e-9);
    ASSERT_NEAR(1.0, u.mixMs, 1e-9);
    ASSERT_NEAR(1.0, u.mixChromaMs, 1e-9);
    // 40 less the six non-dual-iso stages (6) less full20's ten parts (27) = 7 (= 4 + 2 + 1).
    ASSERT_NEAR(7.0, u.totalMs, 1e-9);

    const QString text = totals.fieldsText();
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_ms=6.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_unattributed_ms=7.000")));
    ASSERT_FALSE(text.contains(QStringLiteral("= ")));
}

TEST(ReconWorkerSplit, SummaryLinePinsEveryFieldName)
{
    // The field names on playback_smoke.recon_worker_split_summary (the dual-venue key=value parser and
    // the round report read them by name).
    const QStringList expected = {
        QStringLiteral("llrawproc_total"), QStringLiteral("llrawproc_dark_frame"), QStringLiteral("llrawproc_vertical_stripes"),
        QStringLiteral("llrawproc_focus_pixels"), QStringLiteral("llrawproc_bad_pixels"), QStringLiteral("llrawproc_pattern_noise"),
        QStringLiteral("llrawproc_dual_iso"), QStringLiteral("llrawproc_chroma_smooth"), QStringLiteral("llrawproc_other"),
        QStringLiteral("full20_total"), QStringLiteral("full20_pattern"), QStringLiteral("full20_noise"), QStringLiteral("full20_scratch"),
        QStringLiteral("full20_convert20"), QStringLiteral("full20_match"), QStringLiteral("full20_interp"), QStringLiteral("full20_fullres"),
        QStringLiteral("full20_mix"), QStringLiteral("full20_mix_halfres"), QStringLiteral("full20_mix_chroma"),
        QStringLiteral("full20_mix_chroma_copy"), QStringLiteral("full20_mix_chroma_fullres"), QStringLiteral("full20_mix_chroma_halfres"),
        QStringLiteral("full20_mix_alias_map"), QStringLiteral("full20_mix_overexposed"), QStringLiteral("full20_final_blend"),
        QStringLiteral("full20_convert16"), QStringLiteral("full20_other"),
    };
    ASSERT_EQ(expected.size(), static_cast<int>(recon_worker_split::fields().size()));
    const QString text = recon_worker_split::Totals().fieldsText();
    for (int i = 0; i < expected.size(); ++i)
    {
        ASSERT_TRUE(expected[i] == QLatin1String(recon_worker_split::fields()[static_cast<size_t>(i)].name));
        ASSERT_EQ(1, text.count(QStringLiteral(" avg_%1_ms=").arg(expected[i])));
    }
    for (const char *residue : { "llrawproc_unattributed", "dual_iso_outside_full20", "full20_unattributed",
                                 "full20_mix_unattributed", "full20_mix_chroma_unattributed", "unattributed" })
        ASSERT_EQ(1, text.count(QStringLiteral(" avg_%1_ms=").arg(QLatin1String(residue))));

    // The line is printed once, from the session totals, with frames and full20_valid_frames.
    const QString finish = finishSmokeBody();
    ASSERT_EQ(1, finish.count(QStringLiteral("playback_smoke.recon_worker_split_summary session=%1 frames=%2 ")));
    ASSERT_TRUE(finish.contains(QStringLiteral("+ m_playbackSmokeReconWorkerSplit.fieldsText();")));
    ASSERT_TRUE(notePresentedFrameBody().contains(QStringLiteral("m_playbackSmokeReconWorkerSplit.add( timing );")));
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("m_playbackSmokeReconWorkerSplit.reset();")));
}

TEST(ReconWorkerSplit, RenderThreadKeepsTheWorkersValuesOnAConsumedRecon)
{
    // The worker writes the mix_chroma split too (the render thread wrote it alone before).
    const QString source = renderFrameThreadSource();
    const QString inserter = bracedBlockAfter(source, QStringLiteral("void insertLlrawprocReconTimingTelemetry( QJsonObject &target )"));
    ASSERT_FALSE(inserter.isEmpty());
    for (const char *key : { "dual_iso_full20_mix_chroma_ms", "dual_iso_full20_mix_chroma_copy_ms",
                             "dual_iso_full20_mix_chroma_fullres_ms", "dual_iso_full20_mix_chroma_halfres_ms" })
        ASSERT_EQ(1, inserter.count(QStringLiteral("QStringLiteral(\"%1\")").arg(QLatin1String(key))));

    // drawFrame copies the worker's values, captured before resetMetadata, after the last writer of
    // the unprefixed keys (the GPU no-readback restore) and only on a consumed recon.
    const int restoreAt = source.lastIndexOf(QStringLiteral("restoreGpuPlaybackReconTimingTelemetry(\n            slot.stageTimingTelemetry,"));
    const int copyAt = source.indexOf(QStringLiteral("recon_worker_split::copyWorkerSplit( preservedGpuPlaybackReconTextureTelemetry,"));
    ASSERT_TRUE(restoreAt >= 0);
    ASSERT_TRUE(copyAt > restoreAt);
    ASSERT_EQ(1, source.count(QStringLiteral("recon_worker_split::copyWorkerSplit(")));
    const QString gated = bracedBlockAfter(source.mid(restoreAt), QStringLiteral("    if( decodedRawFrameAlreadyReconned )"));
    ASSERT_TRUE(gated.contains(QStringLiteral("recon_worker_split::copyWorkerSplit( preservedGpuPlaybackReconTextureTelemetry,")));
    // The capture the copy reads is taken before resetMetadata.
    const int preserveAt = source.indexOf(QStringLiteral("preserveGpuPlaybackReconTimingTelemetry(\n            slot.stageTimingTelemetry,\n            preservedGpuPlaybackReconTextureTelemetry );"));
    const int resetAt = source.indexOf(QStringLiteral("slot.resetMetadata();"), preserveAt);
    ASSERT_TRUE(preserveAt >= 0);
    ASSERT_TRUE(resetAt > preserveAt);
    ASSERT_TRUE(copyAt > resetAt);
}

TEST(GuiOtherBusySplit, NestedSpansAreExclusiveAndSumToTheGapsOtherBusy)
{
    // One gap (serial 7) of other busy 20 ms. timer_frame 0..16 holds playback_handling 2..6 (which
    // holds slider_position 3..4) and draw_frame 8..14 (2 ms of preview paint inside it); then
    // draw_frame_ready 17..19 holds 1 ms of runner sleep. Exclusive: slider 1, handling 3, draw 4,
    // timer 16-4-6 = 6, ready 2-1 = 1 -> named 15, unattributed 5.
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    spans.begin(spanClock(7, 0));
    spans.begin(spanClock(7, 2));
    spans.begin(spanClock(7, 3));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::SliderPosition, spanClock(7, 4)), 1e-9);
    ASSERT_NEAR(3.0, spans.end(gui_present_gap::OtherBusySpan::PlaybackHandling, spanClock(7, 6)), 1e-9);
    spans.begin(spanClock(7, 8, 0, 0, 10.0));
    ASSERT_NEAR(4.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(7, 14, 0, 0, 12.0)), 1e-9);
    ASSERT_NEAR(6.0, spans.end(gui_present_gap::OtherBusySpan::TimerFrame, spanClock(7, 16, 0, 0, 12.0)), 1e-9);
    spans.begin(spanClock(7, 17, 0, 0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrameReady, spanClock(7, 19, 0, 1)), 1e-9);
    ASSERT_EQ(0, spans.depth());
    ASSERT_NEAR(5.0, spans.closeGap(20.0), 1e-9);
    ASSERT_NEAR(6.0, spans.sumMs[static_cast<int>(gui_present_gap::OtherBusySpan::TimerFrame)], 1e-9);
    ASSERT_NEAR(1.0, spans.sumMs[static_cast<int>(gui_present_gap::OtherBusySpan::SliderPosition)], 1e-9);
    ASSERT_NEAR(5.0, spans.unattributedSumMs, 1e-9);
}

TEST(GuiOtherBusySplit, SpanAcrossAGapEdgeOrOutsideAGapIsDropped)
{
    gui_present_gap::OtherBusySpans spans;
    // Outside any gap (serial 0).
    spans.begin(spanClock(0, 0));
    ASSERT_NEAR(0.0, spans.end(gui_present_gap::OtherBusySpan::TimerFrame, spanClock(0, 5)), 1e-9);
    // Opened in gap 3, ended in gap 4 (the gap closed and reopened inside it): dropped, and its time
    // does not reach a parent either.
    spans.begin(spanClock(4, 0));
    spans.begin(spanClock(3, 1));
    ASSERT_NEAR(0.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrameReady, spanClock(4, 9)), 1e-9);
    ASSERT_NEAR(10.0, spans.end(gui_present_gap::OtherBusySpan::TimerFrame, spanClock(4, 10)), 1e-9);
    // Named spans never exceed the gap's other busy: 10 named against 4 other busy is scaled to 4.
    ASSERT_NEAR(0.0, spans.closeGap(4.0), 1e-9);
    ASSERT_NEAR(4.0, spans.sumMs[static_cast<int>(gui_present_gap::OtherBusySpan::TimerFrame)], 1e-9);
    // An unbalanced end is ignored.
    ASSERT_NEAR(0.0, spans.end(gui_present_gap::OtherBusySpan::TimerFrame, spanClock(5, 1)), 1e-9);
    ASSERT_EQ(0, spans.depth());
}

TEST(GuiOtherBusySplit, HandlersOpenTheirSpanFirstAndTheSummaryAppendsTheSplit)
{
    const QString source = mainWindowSource();
    const struct { const char *signature; const char *span; } handlers[] = {
        { "void MainWindow::drawFrameReady()\n{\n", "DrawFrameReady" },
        { "void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )\n{\n", "TimerFrame" },
        { "void MainWindow::playbackHandling(int timeDiff)\n{\n", "PlaybackHandling" },
        { "void MainWindow::drawFrame( bool updateTimecodeLabel )\n{\n", "DrawFrame" },
        { "void MainWindow::on_horizontalSliderPosition_valueChanged(int position)\n{\n", "SliderPosition" },
    };
    for (const auto &h : handlers)
    {
        const QString opening = QLatin1String(h.signature)
            + QStringLiteral("    const GuiOtherBusyScope otherBusySpan( this, gui_present_gap::OtherBusySpan::%1 );")
                  .arg(QLatin1String(h.span));
        ASSERT_EQ(1, source.count(opening));
    }
    // The gap's serial moves on every open, and the close folds the spans with the gap's other busy.
    const QString open = bracedBlockAfter(source, QStringLiteral("void MainWindow::openGuiPresentGap( void )"));
    ASSERT_TRUE(open.contains(QStringLiteral("++m_guiPresentGapSerial;")));
    ASSERT_TRUE(open.contains(QStringLiteral("m_guiOtherBusySpans.openGap();")));
    ASSERT_TRUE(closeGapBody().contains(QStringLiteral("m_guiOtherBusySpans.closeGap( split.otherBusyMs );")));
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("m_guiOtherBusySpans.resetSession();")));

    // Appended after every existing gui_present_summary field (none renamed): one per span, then unattributed.
    const QString finish = finishSmokeBody();
    const QString format = summaryFormat(finish, QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    ASSERT_TRUE(format.contains(QStringLiteral("gui_idle_source=%15\" )")));
    ASSERT_FALSE(format.contains(QStringLiteral("avg_gui_other_busy_timer_frame")));
    ASSERT_TRUE(finish.contains(QStringLiteral("QStringLiteral( \" avg_gui_other_busy_%1_ms=%2\" )")));
    ASSERT_TRUE(finish.contains(QStringLiteral("QStringLiteral( \" avg_gui_other_busy_unattributed_ms=%1\" )")));
    ASSERT_TRUE(finish.contains(QStringLiteral("               + otherBusySplit;")));
    const QStringList names = { QStringLiteral("draw_frame_ready"), QStringLiteral("timer_frame"), QStringLiteral("playback_handling"),
                                QStringLiteral("draw_frame"), QStringLiteral("slider_position") };
    ASSERT_EQ(names.size(), gui_present_gap::OtherBusySpans::kCount);
    for (int i = 0; i < names.size(); ++i)
        ASSERT_TRUE(names[i] == QLatin1String(gui_present_gap::otherBusySpanName(static_cast<gui_present_gap::OtherBusySpan>(i))));
}

TEST(GuiPresentAttributionWiring, RegionImageSplitsIntoFromImageAndViewport)
{
    const QString body = presentPreparedBody();
    ASSERT_FALSE(body.isEmpty());
    // Each call is timed on the region clock, and only the cache-miss fromImage is charged.
    const int vpStart = body.indexOf(QStringLiteral("const qint64 prepRegionImageViewportStartNs = prepRegionClock.nsecsElapsed();"));
    const int vpCall = body.indexOf(QStringLiteral("imagePresentedByViewport = GpuDisplayViewport::presentImage( ui->graphicsView,"));
    const int vpEnd = body.indexOf(QStringLiteral("prepRegionImageViewportMs = prepRegionElapsedMs( prepRegionImageViewportStartNs );"));
    ASSERT_TRUE(vpStart >= 0);
    ASSERT_TRUE(vpCall > vpStart);
    ASSERT_TRUE(vpEnd > vpCall);
    const QString miss = bracedBlockAfter(body, QStringLiteral("            pic = cachedPixmap;\n        }\n        else"));
    ASSERT_TRUE(miss.contains(QStringLiteral("pic = QPixmap::fromImage( displayImage );")));
    ASSERT_TRUE(miss.contains(QStringLiteral("prepRegionImageFromImageMs = prepRegionElapsedMs( prepRegionImageFromImageStartNs );")));
    ASSERT_EQ(1, body.count(QStringLiteral("QPixmap::fromImage( displayImage )")));
    for (const char *key : { "playback_prep_region_image_from_image_ms", "playback_prep_region_image_viewport_ms" })
        ASSERT_EQ(1, body.count(QStringLiteral("QStringLiteral(\"%1\")").arg(QLatin1String(key))));

    // Summed on CPU-route frames and averaged on prep_summary, appended after the existing fields.
    const QString gated = bracedBlockAfter(
        notePresentedFrameBody(), QStringLiteral("if( telemetryBoolValue( timing, \"playback_prep_cpu_route_active\" ) )"));
    ASSERT_TRUE(gated.contains(QStringLiteral("spans.regionImageFromImageMs +=")));
    ASSERT_TRUE(gated.contains(QStringLiteral("spans.regionImageViewportMs +=")));
    const QString format = summaryFormat(finishSmokeBody(), QStringLiteral("playback_smoke.prep_summary session=%1"));
    ASSERT_TRUE(format.indexOf(QStringLiteral("avg_playback_prep_region_unattributed_ms=%36 "))
                < format.indexOf(QStringLiteral("avg_playback_prep_region_image_from_image_ms=%37 ")));
    ASSERT_EQ(1, format.count(QStringLiteral("avg_playback_prep_region_image_viewport_ms=%38")));
    ASSERT_TRUE(finishSmokeBody().contains(QStringLiteral(".arg( avgCpuRouteMs( spans.regionImageViewportMs ), 0, 'f', 3 );")));
}