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
#include "../../platform/qt/GuiHopWaitSplit.h"
#include "../../platform/qt/PlaybackPrepOwnership.h"
#include "../../platform/qt/PreviewPaintTiming.h"
#include "../../platform/qt/ReconWorkerSplit.h"

#include <QJsonObject>

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <cmath>
#include <limits>
#include <vector>

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
    // Adversary A1 (400865b1): a runner QThread::msleep never reaches the dispatcher's aboutToBlock,
    // so it was charged as GUI busy. (The measured gui-smoke loop's tail no longer sleeps at all since
    // CPU-S1-GUI-HOP-SLEEP-AND-PRESENT-1: it waits for events, see MeasuredLoopWaitsForEventsNotASleep.)
    // Every other runner sleep goes through the accounting helper, the gap keeps
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
    // mix 10 final_blend 4 convert16 1 (+1 residue). The mix / mix_chroma leaves are reported as given (no residue).
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
    // 40 less the six non-dual-iso stages (6) less full20's ten parts (27) = 7 (= 4 + 2 + 1).
    ASSERT_NEAR(7.0, u.totalMs, 1e-9);

    const QString text = totals.fieldsText();
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_ms=6.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_unattributed_ms=7.000")));
    ASSERT_FALSE(text.contains(QStringLiteral("= ")));
}

TEST(ReconWorkerSplit, NoResidueIsComputedUnderMixOrMixChroma)
{
    // r1 adversary A1 (exact input): a consumed CPU full20 recon with chroma_smooth_method=1 and
    // use_fullres=1. The CPU producer runs the chroma smoothing BEFORE mix (dualiso.c:6988), times its
    // copy outside mix_chroma (:4708-4711) and adds the post-mix fullres reconstruction to
    // mix_chroma_fullres (:7059-7069), so neither mix nor mix_chroma is the parent of those leaves:
    // a residue under either is wrong (it read 0 for both, the producer trace gives 1 and 1).
    QJsonObject frame;
    const auto put = [&frame](const char *key, double v) {
        frame.insert(recon_worker_split::prefix() + QLatin1String(key), v);
    };
    put("llrawproc_total_ms", 50);
    put("dual_iso_full20_mix_ms", 5); put("dual_iso_full20_mix_halfres_ms", 3); put("dual_iso_full20_mix_alias_map_ms", 0);
    put("dual_iso_full20_mix_overexposed_ms", 1); put("dual_iso_full20_mix_chroma_ms", 20);
    put("dual_iso_full20_mix_chroma_copy_ms", 2); put("dual_iso_full20_mix_chroma_fullres_ms", 14);
    put("dual_iso_full20_mix_chroma_halfres_ms", 7);
    recon_worker_split::Totals totals;
    ASSERT_TRUE(totals.add(frame));
    const QString text = totals.fieldsText();
    ASSERT_FALSE(text.contains(QStringLiteral("mix_unattributed")));
    ASSERT_FALSE(text.contains(QStringLiteral("mix_chroma_unattributed")));
    // The leaves are reported as measured.
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_ms=5.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_ms=20.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_copy_ms=2.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_fullres_ms=14.000")));
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_full20_mix_chroma_halfres_ms=7.000")));
    // The fixed-nesting residues use mix only as one of full20's ten parts: 50 - 0 - 5 = 45.
    ASSERT_TRUE(text.contains(QStringLiteral(" avg_unattributed_ms=45.000")));
    // The header names the path dependence, so the next reader does not re-add the residue.
    const QString header = readRepoFile(QStringLiteral("platform/qt/ReconWorkerSplit.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("PATH-DEPENDENT nesting, so NO residue is computed under mix or mix_chroma")));
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
    for (const char *residue : { "llrawproc_unattributed", "dual_iso_outside_full20", "full20_unattributed", "unattributed" })
        ASSERT_EQ(1, text.count(QStringLiteral(" avg_%1_ms=").arg(QLatin1String(residue))));
    ASSERT_EQ(static_cast<int>(recon_worker_split::fields().size()) + 4, static_cast<int>(text.count(QStringLiteral(" avg_"))));

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
    const QString gated = bracedBlockAfter(source.mid(restoreAt), QStringLiteral("    if( renderConsumedWorkerRecon )"));
    ASSERT_TRUE(gated.contains(QStringLiteral("recon_worker_split::copyWorkerSplit( preservedGpuPlaybackReconTextureTelemetry,")));
    // r1 adversary pass 2 A1: the gate is the render CONSUMING the worker's recon, never the slot
    // merely holding one (a refused reuse renders its own). Set at exactly the three consuming sites.
    ASSERT_EQ(1, source.count(QStringLiteral("bool renderConsumedWorkerRecon = false;")));
    ASSERT_EQ(3, source.count(QStringLiteral("renderConsumedWorkerRecon =")) - 1 /* the declaration */);
    ASSERT_EQ(1, source.count(QStringLiteral("renderConsumedWorkerRecon = decodedRawFrameAlreadyReconned;")));
    ASSERT_EQ(1, source.count(QStringLiteral("renderConsumedWorkerRecon =\n            consumedPhase3Recon && decodedRawFrame != nullptr && decodedRawFrameAlreadyReconned;")));
    ASSERT_EQ(1, source.count(QStringLiteral("renderConsumedWorkerRecon =\n            decodedRawFrame && decodedRawFrameAlreadyReconned && renderedFromPhase3Raw;")));
    // The first is inside the GPU no-readback branch (no CPU recon there at all).
    const QString gpuNoReadback = bracedBlockAfter(source, QStringLiteral("    bool renderConsumedWorkerRecon = false;\n    if( skipCpuDebayerForGpuTextureNoReadback )"));
    ASSERT_TRUE(gpuNoReadback.contains(QStringLiteral("renderConsumedWorkerRecon = decodedRawFrameAlreadyReconned;")));
    // The capture the copy reads is taken before resetMetadata.
    const int preserveAt = source.indexOf(QStringLiteral("preserveGpuPlaybackReconTimingTelemetry(\n            slot.stageTimingTelemetry,\n            preservedGpuPlaybackReconTextureTelemetry );"));
    const int resetAt = source.indexOf(QStringLiteral("slot.resetMetadata();"), preserveAt);
    ASSERT_TRUE(preserveAt >= 0);
    ASSERT_TRUE(resetAt > preserveAt);
    ASSERT_TRUE(copyAt > resetAt);
}

TEST(GuiOtherBusySplit, NestedSpansAreExclusiveAndSumToTheGapsOtherBusy)
{
    // One gap (serial 7) of other busy 20 ms; the paint sink's running sum is 10 at the gap's open.
    // timer_frame 0..16 holds playback_handling 2..6 (which holds slider_position 3..4) and draw_frame
    // 8..14 (2 ms of preview paint inside it: the sum goes 10 -> 12); then draw_frame_ready 17..19 holds
    // 1 ms of runner sleep. Inclusive: timer 16-2 = 14, handling 4, draw 6-2 = 4. Exclusive: slider 1,
    // handling 3, draw 4, timer 14-4-4 = 6, ready 2-1 = 1 -> named 15, unattributed 5.
    // (r1 adversary A2: the timer's start must carry the same paint sum, 10, as every reading before
    // the draw; with 0 there it charged 12 ms of paint to the timer and its exclusive time read 0.)
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    spans.begin(spanClock(7, 0, 0, 0, 10.0));
    spans.begin(spanClock(7, 2, 0, 0, 10.0));
    spans.begin(spanClock(7, 3, 0, 0, 10.0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::SliderPosition, spanClock(7, 4, 0, 0, 10.0)), 1e-9);
    ASSERT_NEAR(3.0, spans.end(gui_present_gap::OtherBusySpan::PlaybackHandling, spanClock(7, 6, 0, 0, 10.0)), 1e-9);
    spans.begin(spanClock(7, 8, 0, 0, 10.0));
    ASSERT_NEAR(4.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(7, 14, 0, 0, 12.0)), 1e-9);
    ASSERT_NEAR(6.0, spans.end(gui_present_gap::OtherBusySpan::TimerFrame, spanClock(7, 16, 0, 0, 12.0)), 1e-9);
    spans.begin(spanClock(7, 17, 0, 0, 12.0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrameReady, spanClock(7, 19, 0, 1, 12.0)), 1e-9);
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

TEST(GuiOtherBusySplit, EndWithCountOrPastItNeverWritesPastGapMs)
{
    // CPU-S1-PACE-NEXT-LEVER-1 (#770 r3 advisory A1): end() indexed gapMs with the span unchecked, so
    // OtherBusySpan::Count (or anything past it) wrote gapMs[kCount], which is sumMs[0] in this layout.
    // An out-of-range span still pops its frame (the stack stays balanced) but charges nothing.
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    for (int past = 0; past < 2; ++past)
    {
        spans.begin(spanClock(7, 0));
        const auto bad = static_cast<gui_present_gap::OtherBusySpan>(gui_present_gap::OtherBusySpans::kCount + past);
        ASSERT_NEAR(0.0, spans.end(bad, spanClock(7, 5)), 1e-9);
        ASSERT_EQ(0, spans.depth());
    }
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
    {
        ASSERT_NEAR(0.0, spans.gapMs[i], 1e-9);
        ASSERT_NEAR(0.0, spans.sumMs[i], 1e-9);
    }
    // A valid span still charges as before.
    spans.begin(spanClock(7, 10));
    ASSERT_NEAR(3.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(7, 13)), 1e-9);
}

TEST(GuiOtherBusySplit, NegativeOtherBusyBudgetNeverMakesASessionSumNegative)
{
    // Adversary A1 on 9bfdf8e0: closeGap(-1) scaled 1 ms of named span by -1 into sumMs.
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    spans.begin(spanClock(7, 0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(7, 1)), 1e-9);
    const double rest = spans.closeGap(-1.0);
    ASSERT_TRUE(rest >= 0.0);
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(spans.sumMs[i] >= 0.0);
    ASSERT_TRUE(spans.unattributedSumMs >= 0.0);
}

TEST(GuiOtherBusySplit, NanPaintReadingOrBudgetNeverReachesASessionSum)
{
    // CPU-S1-PACE-NEXT-LEVER-1 r3, adversary A1 on f9f92e09: a NaN paintMs passed both `x < 0` clamps
    // (end) and a NaN budget passed closeGap's, so NaN entered gapMs and then sumMs.
    const double nan = std::numeric_limits<double>::quiet_NaN();
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    gui_present_gap::SpanClock start;
    start.gapSerial = 7;
    gui_present_gap::SpanClock stop = start;
    stop.nowNs = 1000000;
    stop.paintMs = nan;
    spans.begin(start);
    const double charged = spans.end(gui_present_gap::OtherBusySpan::DrawFrame, stop);
    ASSERT_TRUE(std::isfinite(charged) && charged == 0.0);
    const double rest = spans.closeGap(-1.0);
    ASSERT_TRUE(std::isfinite(rest) && rest >= 0.0);
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(std::isfinite(spans.sumMs[i]) && spans.sumMs[i] >= 0.0);
    // A NaN budget is no budget (r4, Dot nit): the named 1 ms span is charged ZERO, so no session sum
    // and no unattributed remainder moves. (Finite/nonnegative alone also passed the old NaN scale.)
    double sumsBefore[gui_present_gap::OtherBusySpans::kCount];
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i) sumsBefore[i] = spans.sumMs[i];
    const double unattributedBefore = spans.unattributedSumMs;
    spans.openGap();
    spans.begin(spanClock(8, 0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(8, 1)), 1e-9);
    const double rest2 = spans.closeGap(nan);
    ASSERT_TRUE(rest2 == 0.0);
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(spans.sumMs[i] == sumsBefore[i]);
    ASSERT_TRUE(spans.unattributedSumMs == unattributedBefore);
    // The zero-budget contract the NaN case maps to: same zero charge.
    spans.openGap();
    spans.begin(spanClock(10, 0));
    ASSERT_NEAR(1.0, spans.end(gui_present_gap::OtherBusySpan::DrawFrame, spanClock(10, 1)), 1e-9);
    ASSERT_TRUE(spans.closeGap(0.0) == 0.0);
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(spans.sumMs[i] == sumsBefore[i]);
    ASSERT_TRUE(spans.unattributedSumMs == unattributedBefore);
    // Adversary A4 on e7e5271e: paintMs = -Infinity made the span +Infinity, closeGap(1.0) scaled it by
    // 1/Infinity = 0 and Infinity * 0 wrote NaN into sumMs.
    spans.openGap();
    gui_present_gap::SpanClock infStart;
    infStart.gapSerial = 9;
    gui_present_gap::SpanClock infStop = infStart;
    infStop.nowNs = 1000000;
    infStop.paintMs = -std::numeric_limits<double>::infinity();
    spans.begin(infStart);
    const double infCharged = spans.end(gui_present_gap::OtherBusySpan::DrawFrame, infStop);
    ASSERT_TRUE(std::isfinite(infCharged));
    const double rest3 = spans.closeGap(1.0);
    ASSERT_TRUE(std::isfinite(rest3) && rest3 >= 0.0);
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(std::isfinite(spans.sumMs[i]) && spans.sumMs[i] >= 0.0);
    ASSERT_TRUE(std::isfinite(spans.closeGap(std::numeric_limits<double>::infinity())));
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(std::isfinite(spans.sumMs[i]));
}

TEST(GuiOtherBusySplit, OverflowingFiniteSpansNeverPutNanInASessionSum)
{
    // r4, adversary A3 on ebe2c4aa (and r3 advisory A2): two finite spans of ~1e308 ms (a synthetic -1e308
    // paint reading) summed to +Infinity in gapMs; closeGap(1.0) scaled by 0 and Infinity * 0 wrote NaN.
    gui_present_gap::OtherBusySpans spans;
    spans.openGap();
    gui_present_gap::SpanClock start;
    start.gapSerial = 7;
    gui_present_gap::SpanClock stop = start;
    stop.nowNs = 1000000;
    stop.paintMs = -1e308;
    for (int i = 0; i < 2; ++i)
    {
        spans.begin(start);
        ASSERT_TRUE(std::isfinite(spans.end(gui_present_gap::OtherBusySpan::DrawFrame, stop)));
    }
    const double rest = spans.closeGap(1.0);
    ASSERT_TRUE(std::isfinite(rest));
    for (int i = 0; i < gui_present_gap::OtherBusySpans::kCount; ++i)
        ASSERT_TRUE(std::isfinite(spans.sumMs[i]) && spans.sumMs[i] >= 0.0 && spans.sumMs[i] <= 1.0 + 1e-9);
    ASSERT_TRUE(std::isfinite(spans.unattributedSumMs));
}

namespace
{
// The innermost { ... } block of `body` that contains offset `at` (empty when unbalanced).
QString enclosingBlock(const QString & body, int at)
{
    int depth = 0;
    int open = -1;
    for (int i = at; i >= 0; --i)
    {
        if (body.at(i) == QLatin1Char('}')) ++depth;
        else if (body.at(i) == QLatin1Char('{') && depth-- == 0) { open = i; break; }
    }
    if (open < 0) return QString();
    depth = 0;
    for (int i = open; i < body.length(); ++i)
    {
        if (body.at(i) == QLatin1Char('{')) ++depth;
        else if (body.at(i) == QLatin1Char('}') && --depth == 0) return body.mid(open, i - open + 1);
    }
    return QString();
}
}

// CPU-S1-PACE-NEXT-LEVER-1 r3, adversary A1 on e7e5271e (major): only a PRESENTED result gave its 8-bit
// buffer back to m_playbackPrepRgb8Pool; a result the worker replaced (two finished before the GUI picked
// one up), dropped on a generation change or at stop, or the GUI dropped, freed it, so the replace-before-
// present pattern still allocated 12.3 MB per pair. Pinned: every place a computed result is dropped
// (its request serial released or collected for release) gives that same result's buffer back in the
// same block.
TEST(PlaybackPrepRgb8Pool, EveryDroppedOrPresentedResultGivesItsBufferBack)
{
    const QString source = mainWindowSource();
    struct Fn { const char * signature; int minDrops; };
    const Fn fns[] = {
        { "void MainWindow::playbackPrepThreadLoop( void )", 3 },   // generation drop, stop, replaced-after-compute
        { "void MainWindow::onPlaybackPrepResultReady( void )", 1 },   // GUI generation drop
        { "void MainWindow::invalidatePlaybackPrepForDisplayChange( const char *reason )", 1 },   // queued results
    };
    const QRegularExpression drop(QStringLiteral(
        "(?:releasePresentedFrameForRequestSerial\\(\\s*|serialsToRelease\\.push_back\\(\\s*)(result|staleResult)\\.task\\.requestSerial"));
    for (const Fn & fn : fns)
    {
        const QString body = bracedBlockAfter(source, QString::fromLatin1(fn.signature));
        if (body.isEmpty()) ::minitest::fail(__FILE__, __LINE__, "function body found", fn.signature);
        int drops = 0;
        auto it = drop.globalMatch(body);
        while (it.hasNext())
        {
            const QRegularExpressionMatch m = it.next();
            ++drops;
            const QString block = enclosingBlock(body, static_cast<int>(m.capturedStart()));
            const QString give = QStringLiteral("m_playbackPrepRgb8Pool.give( std::move( %1.scopeSourceImage ), %1.task.presentationGeneration )").arg(m.captured(1));
            if (!block.contains(give))
                ::minitest::fail(__FILE__, __LINE__, "dropped result gives its buffer back in the same block",
                                 std::string(fn.signature) + " at: " + body.mid(m.capturedStart(), 90).toStdString());
        }
        if (drops < fn.minDrops)
            ::minitest::fail(__FILE__, __LINE__, "drop sites found", std::string(fn.signature) + " drops=" + std::to_string(drops));
    }
    // The presented result, after the present.
    const QString ready = prepResultReadyBody();
    const int present = ready.indexOf(QStringLiteral("presentPlaybackPreparedFrame( result );"));
    ASSERT_TRUE(present >= 0);
    ASSERT_TRUE(ready.indexOf(QStringLiteral("m_playbackPrepRgb8Pool.give( std::move( result.scopeSourceImage ), result.task.presentationGeneration );"), present) > present);
}

// Adversary A2 on e7e5271e (minor): the pool was cleared only on clip open, so buffers survived stop,
// delete-clip and rename-clip. r4 (Dot B1, adversary A1 on 391464de, major): a clear that ran before its
// path's generation bump, or a take() that reopened the pool, let a task admitted before a stop or unload
// keep its buffer after the drain. Pinned: play-stop, clip replacement and both unload paths clear with
// the current generation, after that path's bump and before its drain.
TEST(PlaybackPrepRgb8Pool, StopAndClipUnloadClearThePool)
{
    const QString source = mainWindowSource();
    const QString clearCall = QStringLiteral("m_playbackPrepRgb8Pool.clear( m_playbackPresentationGeneration.load( std::memory_order_acquire ) );");
    const QString toggled = bracedBlockAfter(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"));
    ASSERT_FALSE(toggled.isEmpty());
    const int toggleBump = toggled.indexOf(QStringLiteral("invalidatePlaybackPrepForDisplayChange("));
    // r4 (adversary A1 on ebe2c4aa): play-start opens the pool with its own (post-bump) generation.
    const int toggleOpen = toggled.indexOf(QStringLiteral("if( checked ) m_playbackPrepRgb8Pool.open( m_playbackPresentationGeneration.load( std::memory_order_acquire ) );"));
    ASSERT_TRUE(toggleOpen > toggleBump);
    const int toggleClear = toggled.indexOf(QStringLiteral("else ") + clearCall);
    const int toggleDrain = toggled.indexOf(QStringLiteral("drainPlaybackPrepInFlight( \"play-stop\""));
    ASSERT_TRUE(toggleBump >= 0 && toggleClear > toggleBump && toggleDrain > toggleClear);

    const QString replace = bracedBlockAfter(source, QStringLiteral("bool MainWindow::playbackPrepQuiescentBeforeClipReplacement( const char *reason )"));
    ASSERT_FALSE(replace.isEmpty());
    const int replaceBump = replace.indexOf(QStringLiteral("invalidatePlaybackPrepForDisplayChange( reason );"));
    const int replaceClear = replace.indexOf(clearCall);
    const int replaceDrain = replace.indexOf(QStringLiteral("drainPlaybackPrepInFlight("));
    ASSERT_TRUE(replaceBump >= 0 && replaceClear > replaceBump && replaceDrain > replaceClear);

    const QString unload = bracedBlockAfter(source, QStringLiteral("bool MainWindow::freeActiveMlvObjectAfterLifecycleBarrier( const char *reason )"));
    ASSERT_FALSE(unload.isEmpty());
    // No-object path: clear, then the drain it returns.
    const int early = unload.indexOf(QStringLiteral("if( !m_pMlvObject )"));
    const int earlyClear = unload.indexOf(clearCall, early);
    const int earlyDrain = unload.indexOf(QStringLiteral("drainPlaybackPrepInFlight( reason, 2000 )"), early);
    ASSERT_TRUE(early >= 0 && earlyClear > early && earlyDrain > earlyClear);
    // Object path: clearPresentationForClipOpen (its bump), then the clear, then the drain.
    const int unloadBump = unload.indexOf(QStringLiteral("clearPresentationForClipOpen( barrierReason );"));
    const int unloadClear = unload.indexOf(clearCall, unloadBump);
    const int unloadDrain = unload.indexOf(QStringLiteral("waitForRenderThreadIdleBeforeCoreMutation( barrierReason )"), unloadBump);
    ASSERT_TRUE(unloadBump > earlyDrain && unloadClear > unloadBump && unloadDrain > unloadClear);
    const QString open = bracedBlockAfter(source, QStringLiteral("void MainWindow::clearPresentationForClipOpen( const char *reason )"));
    ASSERT_TRUE(open.contains(QStringLiteral("invalidatePlaybackPrepForDisplayChange( reason );")));
}

// r4 (Dot B1): the pool is keyed on the presentation generation. Every give passes the generation of
// the task whose buffer it returns, every clear passes the current generation, and take() takes no
// argument (it never reopens the pool). A new give or clear site without its generation fails here.
TEST(PlaybackPrepRgb8Pool, EveryGivePassesItsTaskGenerationAndEveryClearTheCurrentOne)
{
    const QString source = mainWindowSource();
    const QRegularExpression anyGive(QStringLiteral("m_playbackPrepRgb8Pool\\.give\\("));
    const QRegularExpression keyedGive(QStringLiteral(
        "m_playbackPrepRgb8Pool\\.give\\( std::move\\( (\\w+)\\.scopeSourceImage \\), \\1\\.task\\.presentationGeneration \\);"));
    int gives = 0;
    for (auto it = anyGive.globalMatch(source); it.hasNext(); it.next()) ++gives;
    int keyed = 0;
    for (auto it = keyedGive.globalMatch(source); it.hasNext(); it.next()) ++keyed;
    if (gives < 6 || keyed != gives)
        ::minitest::fail(__FILE__, __LINE__, "every pool give passes its task generation",
                         "gives=" + std::to_string(gives) + " keyed=" + std::to_string(keyed));
    const QString keyedClear = QStringLiteral("m_playbackPrepRgb8Pool.clear( m_playbackPresentationGeneration.load( std::memory_order_acquire ) );");
    const int clears = static_cast<int>(source.count(QStringLiteral("m_playbackPrepRgb8Pool.clear(")));
    const int keyedClears = static_cast<int>(source.count(keyedClear));
    if (clears < 4 || keyedClears != clears)
        ::minitest::fail(__FILE__, __LINE__, "every pool clear passes the current generation",
                         "clears=" + std::to_string(clears) + " keyed=" + std::to_string(keyedClears));
    const int takes = static_cast<int>(source.count(QStringLiteral("m_playbackPrepRgb8Pool.take(")));
    ASSERT_TRUE(takes >= 1 && takes == static_cast<int>(source.count(QStringLiteral("m_playbackPrepRgb8Pool.take()"))));
    // The pool header: generation-keyed give/clear, no reopen flag.
    const QString h = readRepoFile(QStringLiteral("platform/qt/PlaybackPrepCpuRoute.h"));
    ASSERT_TRUE(h.contains(QStringLiteral("void give(std::vector<uint8_t> && buf, uint64_t generation);")));
    ASSERT_TRUE(h.contains(QStringLiteral("void clear(uint64_t currentGeneration);")));
    ASSERT_TRUE(h.contains(QStringLiteral("void open(uint64_t playGeneration);")));
    // The only open is the play-start one (a second site would reopen recycling outside a playing span).
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("m_playbackPrepRgb8Pool.open("))));
    ASSERT_FALSE(h.contains(QStringLiteral("m_closed")));
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
    // CPU-S1-PACE-NEXT-LEVER-1 appends its fields after this one (PrepSummaryAppendsTheHandoffFields).
    ASSERT_TRUE(finishSmokeBody().contains(QStringLiteral(".arg( avgCpuRouteMs( spans.regionImageViewportMs ), 0, 'f', 3 )\n               + handoffFields;")));
}

// ---------------------------------------------------------------------------------------------
// CPU-S1-PACE-NEXT-LEVER-1 (Part A): the CPU playing route borrows the pinned slot's RGB16 frame.
// ---------------------------------------------------------------------------------------------
namespace
{

QString drawFrameReadyBody()
{
    return functionBody(mainWindowSource(), QStringLiteral("void MainWindow::drawFrameReady()\n{"),
                        QStringLiteral("//Paintmode for gradient enabled/disabled"));
}

QString enqueueBody()
{
    return functionBody(mainWindowSource(),
                        QStringLiteral("void MainWindow::enqueuePlaybackPrepTask( PlaybackPrepTask &&task, double handoffStartStageTime )"),
                        QStringLiteral("void MainWindow::invalidatePlaybackPrepForDisplayChange( const char *reason )"));
}

QString prepThreadLoopBody()
{
    return functionBody(mainWindowSource(), QStringLiteral("void MainWindow::playbackPrepThreadLoop( void )"),
                        QStringLiteral("void MainWindow::onPlaybackPrepResultReady( void )"));
}

// Every input that admits a borrow; each test case flips one.
PlaybackPrepBorrowInputs admittedBorrow(const uint16_t * buffer)
{
    PlaybackPrepBorrowInputs in;
    in.playing = true;
    in.displayPreviewCachingAllowed = false;
    in.cpuPreviewProcessingActive = true;
    in.outputProcessed8 = false;
    in.rawImage16 = buffer;
    in.rawImage16Bytes = 600;
    in.sourceImage16Bytes = 600;
    return in;
}

} // namespace

TEST(PlaybackPrepOwnership, BorrowTruthTableRefusesEveryGpuRouteAndEveryNonPlayingDisplay)
{
    const uint16_t buffer[300] = {};
    ASSERT_TRUE(playbackPrepMayBorrowSlotRgb16(admittedBorrow(buffer)));
    // The CPU route is either CPU preview processing or a 16-bit output mode.
    {
        PlaybackPrepBorrowInputs in = admittedBorrow(buffer);
        in.cpuPreviewProcessingActive = false;
        ASSERT_TRUE(playbackPrepMayBorrowSlotRgb16(in));           // Debayered16 / Processed16 output
        in.outputProcessed8 = true;
        ASSERT_FALSE(playbackPrepMayBorrowSlotRgb16(in));          // neither: the worker reads RGB8
        in.cpuPreviewProcessingActive = true;
        ASSERT_TRUE(playbackPrepMayBorrowSlotRgb16(in));           // Processed8 + CPU processing
    }
    // One refusal per input; the full product of the eight boolean refusals below is also refused.
    const struct { const char * name; void (*flip)(PlaybackPrepBorrowInputs &); } refusals[] = {
        { "paused", [](PlaybackPrepBorrowInputs & in) { in.playing = false; } },
        { "display_cache", [](PlaybackPrepBorrowInputs & in) { in.displayPreviewCachingAllowed = true; } },
        { "gpu16", [](PlaybackPrepBorrowInputs & in) { in.gpu16PreviewActive = true; } },
        { "gpu_preview_processing", [](PlaybackPrepBorrowInputs & in) { in.gpuPreviewProcessingActive = true; } },
        { "amaze_texture", [](PlaybackPrepBorrowInputs & in) { in.gpuAmazeTexturePresentCandidate = true; } },
        { "recon_texture", [](PlaybackPrepBorrowInputs & in) { in.gpuPlaybackReconTexturePresentCandidate = true; } },
        { "recon_no_readback", [](PlaybackPrepBorrowInputs & in) { in.gpuPlaybackReconTextureNoReadbackCandidate = true; } },
        { "no_buffer", [](PlaybackPrepBorrowInputs & in) { in.rawImage16 = nullptr; } },
        { "short_buffer", [](PlaybackPrepBorrowInputs & in) { in.rawImage16Bytes = 598; } },
        { "no_dims", [](PlaybackPrepBorrowInputs & in) { in.sourceImage16Bytes = 0; } },
    };
    for (const auto & r : refusals)
    {
        PlaybackPrepBorrowInputs in = admittedBorrow(buffer);
        r.flip(in);
        if (playbackPrepMayBorrowSlotRgb16(in))
            ::minitest::fail(__FILE__, __LINE__, "refused", r.name);
    }
    // Exhaustive over the boolean inputs: true only for the admitted CPU-route rows.
    int admitted = 0;
    for (int bits = 0; bits < (1 << 9); ++bits)
    {
        PlaybackPrepBorrowInputs in = admittedBorrow(buffer);
        in.playing = bits & 1;
        in.displayPreviewCachingAllowed = bits & 2;
        in.cpuPreviewProcessingActive = bits & 4;
        in.outputProcessed8 = bits & 8;
        in.gpu16PreviewActive = bits & 16;
        in.gpuPreviewProcessingActive = bits & 32;
        in.gpuAmazeTexturePresentCandidate = bits & 64;
        in.gpuPlaybackReconTexturePresentCandidate = bits & 128;
        in.gpuPlaybackReconTextureNoReadbackCandidate = bits & 256;
        const bool expected = in.playing && !in.displayPreviewCachingAllowed
            && (in.cpuPreviewProcessingActive || !in.outputProcessed8)
            && !in.gpu16PreviewActive && !in.gpuPreviewProcessingActive
            && !in.gpuAmazeTexturePresentCandidate && !in.gpuPlaybackReconTexturePresentCandidate
            && !in.gpuPlaybackReconTextureNoReadbackCandidate;
        ASSERT_EQ(expected, playbackPrepMayBorrowSlotRgb16(in));
        if (expected) ++admitted;
    }
    ASSERT_EQ(3, admitted);   // CPU processing x {Processed8, 16-bit} + 16-bit without CPU processing
}

TEST(PlaybackPrepOwnership, SampledHashSeesASingleChangedSampleAndCapsTheSamples)
{
    std::vector<uint16_t> frame(1808u * 2268u * 3u);
    for (size_t i = 0; i < frame.size(); ++i) frame[i] = static_cast<uint16_t>(i * 2654435761u >> 7);
    const uint32_t before = playbackPrepSampledRgb16Fnv(frame.data(), frame.size());
    ASSERT_EQ(before, playbackPrepSampledRgb16Fnv(frame.data(), frame.size()));
    const size_t step = frame.size() / 8192;
    frame[step * 4096] ^= 0x0100;   // a sampled word, high byte only
    ASSERT_TRUE(before != playbackPrepSampledRgb16Fnv(frame.data(), frame.size()));
    frame[step * 4096] ^= 0x0100;
    frame[step * 4096] ^= 0x0001;   // low byte only
    ASSERT_TRUE(before != playbackPrepSampledRgb16Fnv(frame.data(), frame.size()));
    // Null / empty input is a fixed value, never a read.
    ASSERT_EQ(2166136261u, playbackPrepSampledRgb16Fnv(nullptr, 100));
    ASSERT_EQ(2166136261u, playbackPrepSampledRgb16Fnv(frame.data(), 0));
}

TEST(PlaybackPrepOwnership, DrawFrameReadyConsultsTheHelperBeforeTheCopyAndMovesTheTask)
{
    const QString body = drawFrameReadyBody();
    ASSERT_FALSE(body.isEmpty());
    const int helperAt = body.indexOf(QStringLiteral("task.borrowedSlotRgb16 = playbackPrepMayBorrowSlotRgb16( borrowInputs );"));
    const int assignAt = body.indexOf(QStringLiteral("task.ownedSourceImage16.assign( readyFrame.rawImage16,"));
    ASSERT_TRUE(helperAt >= 0);
    ASSERT_TRUE(assignAt > helperAt);
    ASSERT_EQ(1, body.count(QStringLiteral("task.ownedSourceImage16.assign(")));
    // The copy is skipped exactly when the helper admits the borrow.
    const int guardAt = body.indexOf(QStringLiteral("    if( !task.borrowedSlotRgb16\n     && needsOwnedRgb16\n"));
    ASSERT_TRUE(guardAt > helperAt && guardAt < assignAt);
    // The slot pointer is what the task already carries; nothing else rebinds it.
    ASSERT_TRUE(body.indexOf(QStringLiteral("task.sourceImage16 = readyFrame.rawImage16;")) < helperAt);
    // Every GPU route input reaches the helper from the ready frame / this present's flags.
    for (const char * wire : { "borrowInputs.playing = ui->actionPlay->isChecked();",
                               "borrowInputs.displayPreviewCachingAllowed = displayPreviewCachingAllowed;",
                               "borrowInputs.cpuPreviewProcessingActive = cpuPreviewProcessingActive;",
                               "borrowInputs.outputProcessed8 = readyFrame.outputMode == RenderFrameThread::OutputProcessed8;",
                               "borrowInputs.gpu16PreviewActive = gpu16PreviewActive;",
                               "borrowInputs.gpuPreviewProcessingActive = gpuPreviewProcessingActive;",
                               "borrowInputs.gpuAmazeTexturePresentCandidate = readyFrame.gpuAmazeTexturePresentCandidate;",
                               "readyFrame.gpuPlaybackReconTexturePresentCandidate;",
                               "readyFrame.gpuPlaybackReconTextureNoReadbackCandidate;",
                               "borrowInputs.rawImage16 = readyFrame.rawImage16;",
                               "borrowInputs.rawImage16Bytes = readyFrameRawImage16Bytes;",
                               "borrowInputs.sourceImage16Bytes = sourceImage16Bytes;" })
    {
        if (!body.contains(QLatin1String(wire))) ::minitest::fail(__FILE__, __LINE__, "helper input wired", wire);
    }
    // The GPU copies are untouched.
    ASSERT_EQ(1, body.count(QStringLiteral("task.ownedGpuAmazeTextureRawFrame.assign(")));
    ASSERT_EQ(1, body.count(QStringLiteral("task.ownedGpuPlaybackReconTextureBayerFrame.assign(")));
    ASSERT_EQ(1, body.count(QStringLiteral("task.ownedGpuPlaybackReconTextureInputBayerFrame.assign(")));
    // Hand-off: moved, with the span start; the fail-closed hash only under smoke telemetry.
    ASSERT_TRUE(body.contains(QStringLiteral("enqueuePlaybackPrepTask( std::move( task ), handoffStartStageTime );")));
    ASSERT_TRUE(body.indexOf(QStringLiteral("const double handoffStartStageTime = mlv_stage_timing_now();")) < helperAt);
    ASSERT_TRUE(body.contains(QStringLiteral("if( task.borrowedSlotRgb16 && m_playbackSmokeFrameTelemetry )")));
}

TEST(PlaybackPrepOwnership, EnqueueAndDequeueMoveNeverCopy)
{
    const QString enqueue = enqueueBody();
    ASSERT_FALSE(enqueue.isEmpty());
    ASSERT_TRUE(enqueue.contains(QStringLiteral("PlaybackPrepTask ownedTask = std::move( task );")));
    ASSERT_TRUE(enqueue.contains(QStringLiteral("m_playbackPrepPending = std::move( ownedTask );")));
    ASSERT_FALSE(enqueue.contains(QStringLiteral("m_playbackPrepPending = ownedTask;")));
    // The replaced-before-compute release still reads the OLD pending serial before the store.
    const int releaseAt = enqueue.indexOf(QStringLiteral("m_playbackPrepPending.requestSerial );"));
    const int storeAt = enqueue.indexOf(QStringLiteral("m_playbackPrepPending = std::move( ownedTask );"));
    ASSERT_TRUE(releaseAt >= 0 && storeAt > releaseAt);
    ASSERT_EQ(2, enqueue.count(QStringLiteral("rebindOwnedImagePointers();")));
    ASSERT_TRUE(enqueue.contains(QStringLiteral("QStringLiteral(\"playback_prep_handoff_ms\")")));
    // The header takes an rvalue: a caller cannot hand it a copy by accident.
    ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h"))
                    .contains(QStringLiteral("void enqueuePlaybackPrepTask( PlaybackPrepTask &&task, double handoffStartStageTime );")));

    const QString loop = prepThreadLoopBody();
    ASSERT_FALSE(loop.isEmpty());
    ASSERT_TRUE(loop.contains(QStringLiteral("task = std::move( m_playbackPrepPending );\n            task.rebindOwnedImagePointers();\n            m_playbackPrepPending = PlaybackPrepTask();")));
    ASSERT_FALSE(loop.contains(QStringLiteral("task = m_playbackPrepPending;")));
    // The worker re-hashes the borrowed frame after its build, while the slot is still pinned.
    const int buildAt = loop.indexOf(QStringLiteral("PlaybackPrepResult result = buildPlaybackPrepResult( task, &threadState );"));
    const int hashAt = loop.indexOf(QStringLiteral("playbackPrepSampledRgb16Fnv( task.sourceImage16, task.borrowedRgb16Words )"));
    const int pushAt = loop.indexOf(QStringLiteral("m_playbackPrepResults.push_back( std::move( result ) );"));
    ASSERT_TRUE(buildAt >= 0 && hashAt > buildAt && pushAt > hashAt);
    ASSERT_TRUE(loop.contains(QStringLiteral("m_playbackPrepBorrowedRgb16MutatedCount.fetch_add( 1, std::memory_order_acq_rel );")));
}

TEST(PlaybackPrepOwnership, PresentingSlotsStayPinnedAndTheOneInitSiteIsDrained)
{
    const QString rft = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    // findFreeSlotLocked never hands a presenting (or ready, or rendering) slot to a render.
    const QString freeSlot = bracedBlockAfter(rft, QStringLiteral("int RenderFrameThread::findFreeSlotLocked() const"));
    ASSERT_TRUE(freeSlot.contains(QStringLiteral("if( slot.ready || slot.presenting ) continue;")));
    ASSERT_TRUE(freeSlot.contains(QStringLiteral("if( i == m_renderingSlotIndex ) continue;")));
    ASSERT_TRUE(freeSlot.contains(QStringLiteral("if( slot.state.load( std::memory_order_acquire ) != SlotState::Idle ) continue;")));
    // Acquire pins it; only the by-serial release unpins it.
    const QString acquire = functionBody(rft, QStringLiteral("bool RenderFrameThread::acquireReadySlotLocked( ReadyFrame *frame,"),
                                         QStringLiteral("void RenderFrameThread::releasePresentedFrame()"));
    ASSERT_TRUE(acquire.contains(QStringLiteral("slot.presenting = true;")));
    ASSERT_TRUE(acquire.contains(QStringLiteral("frame->rawImage16 = slot.rawImage16.empty() ? nullptr : slot.rawImage16.data();")));
    // A playback cancel keeps a Presenting slot pinned.
    ASSERT_TRUE(rft.contains(QStringLiteral("// A Presenting slot stays pinned: its borrower releases it by serial.")));
    // init is the one writer that ignores the pin: it has exactly one caller, after the prep drain
    // and the lifecycle backstop (both abort the open when the worker does not go idle).
    const QString mw = mainWindowSource();
    ASSERT_EQ(1, mw.count(QStringLiteral("m_pRenderThread->init(")));
    for (const QString & file : { QStringLiteral("platform/qt/RenderFrameThread.cpp"), QStringLiteral("platform/qt/GpuDisplayWindow.cpp"),
                                  QStringLiteral("platform/qt/GpuDisplayViewport.cpp") })
        ASSERT_EQ(0, readRepoFile(file).count(QStringLiteral("->init( m_pMlvObject")));
    const QString open = functionBody(mw, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                      QStringLiteral("m_pendingPresentationRequests.clear();"));
    ASSERT_FALSE(open.isEmpty());
    const int quiescentAt = open.indexOf(QStringLiteral("if( !playbackPrepQuiescentBeforeClipReplacement( \"clip-open\" ) )\n    {\n        return kClipOpenAbortedPrepBusy;"));
    const int backstopAt = open.indexOf(QStringLiteral("if( !freeActiveMlvObjectAfterLifecycleBarrier( \"clip-open\" ) )"));
    const int initAt = open.indexOf(QStringLiteral("m_pRenderThread->init( m_pMlvObject,"));
    ASSERT_TRUE(quiescentAt >= 0 && backstopAt > quiescentAt && initAt > backstopAt);
    // Nothing between the backstop and init pumps events (a pump could start a new prep task).
    const QString tail = open.mid(backstopAt, initAt - backstopAt);
    ASSERT_FALSE(tail.contains(QStringLiteral("processEvents")));
    ASSERT_FALSE(tail.contains(QStringLiteral("clearPresentationForClipOpen(")));
    // The guard drops the pending task and drains the in-flight one; the backstop drains again.
    const QString quiescent = bracedBlockAfter(mw, QStringLiteral("bool MainWindow::playbackPrepQuiescentBeforeClipReplacement( const char *reason )"));
    ASSERT_TRUE(quiescent.contains(QStringLiteral("invalidatePlaybackPrepForDisplayChange( reason );")));
    ASSERT_TRUE(quiescent.contains(QStringLiteral("drainPlaybackPrepInFlight( reason, 2000 )")));
    const QString backstop = bracedBlockAfter(mw, QStringLiteral("bool MainWindow::freeActiveMlvObjectAfterLifecycleBarrier( const char *reason )"));
    ASSERT_TRUE(backstop.contains(QStringLiteral("const bool prepMutationAllowed = waitForRenderThreadIdleBeforeCoreMutation( barrierReason );")));
    ASSERT_TRUE(backstop.contains(QStringLiteral("return prepMutationAllowed;")));
}

TEST(PlaybackPrepOwnership, PrepSummaryAppendsTheHandoffFields)
{
    const QString finish = finishSmokeBody();
    // Existing fields keep their names and positions; the new ones ride on handoffFields.
    const QString format = summaryFormat(finish, QStringLiteral("playback_smoke.prep_summary session=%1"));
    ASSERT_TRUE(format.contains(QStringLiteral("avg_playback_prep_region_image_viewport_ms=%38\" )")));
    for (const char * field : { "avg_playback_prep_handoff_ms=%1", "handoff_borrowed_rgb16_frames=%2",
                                "handoff_owned_rgb16_bytes=%3", "handoff_borrowed_rgb16_mutated_frames=%4",
                                "avg_playback_prep_region_image_magenta_ms=%5", "avg_playback_prep_region_image_hash_ms=%6",
                                "avg_playback_prep_region_image_set_pixmap_ms=%7", "avg_playback_prep_region_image_release_ms=%8" })
    {
        if (finish.count(QLatin1String(field)) != 1) ::minitest::fail(__FILE__, __LINE__, "prep_summary field", field);
    }
    ASSERT_TRUE(finish.contains(QStringLiteral("QStringLiteral( \" avg_playback_prep_handoff_ms=%1 ")));
    // The mutation count is the session's delta of the worker's counter.
    ASSERT_TRUE(finish.contains(QStringLiteral("m_playbackPrepBorrowedRgb16MutatedCount.load( std::memory_order_acquire )\n            - m_playbackSmokeStartPrepBorrowedRgb16Mutated;")));
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("m_playbackSmokeStartPrepBorrowedRgb16Mutated =\n        m_playbackPrepBorrowedRgb16MutatedCount.load( std::memory_order_acquire );")));
    // Summed on CPU-route frames from the per-frame keys.
    const QString gated = bracedBlockAfter(
        notePresentedFrameBody(), QStringLiteral("if( telemetryBoolValue( timing, \"playback_prep_cpu_route_active\" ) )"));
    for (const char * key : { "playback_prep_handoff_ms", "playback_prep_handoff_borrowed_rgb16", "playback_prep_owned_rgb16_bytes",
                              "playback_prep_region_image_magenta_ms", "playback_prep_region_image_hash_ms",
                              "playback_prep_region_image_set_pixmap_ms", "playback_prep_region_image_release_ms" })
    {
        if (!gated.contains(QStringLiteral("timing, \"%1\" )").arg(QLatin1String(key))))
            ::minitest::fail(__FILE__, __LINE__, "summed on CPU-route frames", key);
    }
    // Each residual key is produced once in the present.
    const QString present = presentPreparedBody();
    for (const char * key : { "playback_prep_region_image_magenta_ms", "playback_prep_region_image_hash_ms",
                              "playback_prep_region_image_set_pixmap_ms", "playback_prep_region_image_release_ms" })
        ASSERT_EQ(1, present.count(QStringLiteral("QStringLiteral(\"%1\")").arg(QLatin1String(key))));
}

// ---- CPU-S1-PRESENT-INTERVAL-BOUND-1 ------------------------------------------------------------------

TEST(CriticalHopSummaryWiring, NewLineFollowsGuiPresentSummaryWithEveryFieldInOrder)
{
    const QString finish = finishSmokeBody();
    ASSERT_FALSE(finish.isEmpty());
    ASSERT_EQ(1, finish.count(QStringLiteral("\"playback_smoke.critical_hop_summary session=%1 frames=%2 \"")));
    // Printed right after gui_present_summary (its qInfo is the next one) and before recon_worker_split_summary.
    const int gui = finish.indexOf(QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    const int hop = finish.indexOf(QStringLiteral("playback_smoke.critical_hop_summary session=%1"));
    const int recon = finish.indexOf(QStringLiteral("playback_smoke.recon_worker_split_summary session=%1"));
    ASSERT_TRUE(gui > 0);
    ASSERT_TRUE(hop > gui);
    ASSERT_TRUE(recon > hop);
    ASSERT_EQ(1, finish.mid(gui, hop - gui).count(QStringLiteral("qInfo()")));
    const QString format = summaryFormat(finish, QStringLiteral("playback_smoke.critical_hop_summary session=%1"));
    ASSERT_FALSE(format.isEmpty());
    const QStringList fields = {
        QStringLiteral("session"), QStringLiteral("frames"),
        QStringLiteral("avg_frame_ready_wait_ms"), QStringLiteral("avg_frame_ready_wait_runner_sleep_ms"),
        QStringLiteral("avg_frame_ready_wait_prepared_present_ms"), QStringLiteral("avg_frame_ready_wait_viewport_paint_ms"),
        QStringLiteral("avg_frame_ready_wait_other_ms"), QStringLiteral("frame_ready_wait_unattributed_frames"),
        QStringLiteral("avg_prep_result_wait_ms"), QStringLiteral("avg_prep_result_wait_runner_sleep_ms"),
        QStringLiteral("avg_prep_result_wait_viewport_paint_ms"), QStringLiteral("avg_prep_result_wait_other_ms"),
        QStringLiteral("prep_result_wait_unattributed_frames"), QStringLiteral("emit_to_request_frames"),
        QStringLiteral("avg_emit_to_request_issue_ms"), QStringLiteral("avg_upstream_plus_render_per_render_ms"),
        // r1 fix iteration 2 (adversary A2 on df1b864e): appended at the end, nothing before it moves.
        QStringLiteral("avg_prep_result_wait_prepared_present_ms"),
        // CPU-S1-GUI-HOP-SLEEP-AND-PRESENT-1: Gate 0 then the runner's event waits, appended at the end.
        QStringLiteral("present_ui_signal_latency_own_window_frames"),
        QStringLiteral("avg_present_ui_signal_latency_own_window_ms"),
        QStringLiteral("present_ui_signal_latency_overwritten_frames"),
        QStringLiteral("runner_event_waits"), QStringLiteral("runner_event_wait_timeouts"),
        // r2 (adversary A1 on e447bc14): the waits that returned past their bound, appended at the end.
        QStringLiteral("runner_event_wait_overruns"),
    };
    int last = -1;
    for (int i = 0; i < fields.size(); ++i)
    {
        const QString key = QStringLiteral("%1=%%2").arg(fields[i]).arg(i + 1);
        const int at = format.indexOf(key);
        if (at <= last) ::minitest::fail(__FILE__, __LINE__, "critical_hop_summary field order", key.toLatin1().constData());
        last = at;
    }
    ASSERT_EQ(fields.size(), static_cast<int>(format.count(QLatin1Char('='))));
    // S is decode + recon + render busy per render start, from the overlap_summary's own snapshot.
    ASSERT_TRUE(finish.contains(QStringLiteral("hopOverlap = overlap;")));
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "( hopOverlap.decodeBusyMs + hopOverlap.reconBusyMs + hopOverlap.renderBusyMs )\n"
        "                      / static_cast<double>( hopOverlap.renderStarts )")));
    // The gui_present_summary format itself is untouched (rename or reorder nothing).
    const QString gpFormat = summaryFormat(finish, QStringLiteral("playback_smoke.gui_present_summary session=%1"));
    ASSERT_TRUE(gpFormat.contains(QStringLiteral("avg_gui_other_busy_ms=%13 max_gui_other_busy_ms=%14 gui_idle_source=%15\" )")));
}

TEST(CriticalHopSummaryWiring, HooksFeedTheSplitOnlyInsideASmokeSession)
{
    const QString source = mainWindowSource();
    // Runner sleep: [start, end] on the stage clock, only while a session is active.
    const QString sleep = bracedBlockAfter(source, QStringLiteral("void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )"));
    ASSERT_TRUE(sleep.contains(QStringLiteral("const double hopSleepStartS = m_playbackSmokeActive ? mlv_stage_timing_now() : 0.0;")));
    // HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r13 (the merge with #811's due-wake sleep): the interval ends when the
    // sleep ends, before the woken wake runs, and takes out the sent messages the sleep dispatched (dispatchNs), as the other
    // two sleep accounts do.
    ASSERT_TRUE(sleep.contains(QStringLiteral("const double hopSleepEndS = mlv_stage_timing_now();")));
    ASSERT_TRUE(sleep.contains(QStringLiteral(
        "m_guiHopWait.push( gui_hop_wait::Kind::RunnerSleep, hopSleepStartS,\n"
        "                           std::max( hopSleepStartS, hopSleepEndS - static_cast<double>( dispatchNs ) * 1e-9 ) );")));
    ASSERT_EQ(1, sleep.count(QStringLiteral("m_guiHopWait.push(")));
    ASSERT_TRUE(sleep.indexOf(QStringLiteral("m_guiHopWait.push(")) > sleep.indexOf(QStringLiteral("QThread::msleep( ms );")));
    ASSERT_TRUE(sleep.indexOf(QStringLiteral("m_guiHopWait.push(")) > sleep.indexOf(QStringLiteral("const qint64 dispatchNs =")));
    ASSERT_TRUE(sleep.indexOf(QStringLiteral("m_guiHopWait.push(")) < sleep.indexOf(QStringLiteral("m_playbackDueWake->dispatchSentMessages();")));

    // Prepared present: the result's wait split at entry (the avg_playback_prep_result_queue_ms stamp), then the
    // present's own [entry, exit] interval, from one entry stamp.
    const QString present = presentPreparedBody();
    ASSERT_FALSE(present.isEmpty());
    const int entry = present.indexOf(QStringLiteral("const double prepPresentStart = mlv_stage_timing_now();"));
    const int prepSplit = present.indexOf(QStringLiteral("m_guiHopPrepResultWait.add( m_guiHopWait.split( result.resultReadyTime, prepPresentStart ) );"));
    const int scope = present.indexOf(QStringLiteral("const gui_hop_wait::IntervalScope hopPresentInterval(\n        m_playbackSmokeActive ? &m_guiHopWait : nullptr, gui_hop_wait::Kind::PreparedPresent,\n        prepPresentStart, &guiHopWaitClockNow );"));
    ASSERT_TRUE(entry >= 0);
    ASSERT_TRUE(prepSplit > entry);
    ASSERT_TRUE(scope > prepSplit);
    ASSERT_TRUE(present.contains(QStringLiteral("if( m_playbackSmokeActive && result.resultReadyTime > 0.0 && prepPresentStart >= result.resultReadyTime )")));

    // drawFrameReady: frame_ready_wait is the m_lastDrawFrameReadyQueueMs window; emit_to_request on an issuing advance.
    const QString ready = bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(ready.isEmpty());
    const QString queueBlock = bracedBlockAfter(ready, QStringLiteral(
        "if( readyFrame.frameReadyEmitStageTime > 0.0\n     && display_start >= readyFrame.frameReadyEmitStageTime )"));
    ASSERT_TRUE(queueBlock.contains(QStringLiteral("m_lastDrawFrameReadyQueueMs =")));
    ASSERT_TRUE(queueBlock.contains(QStringLiteral(
        "if( m_playbackSmokeActive )\n            m_guiHopFrameReadyWait.add( m_guiHopWait.split( readyFrame.frameReadyEmitStageTime, display_start ) );")));
    const int before = ready.indexOf(QStringLiteral("const uint64_t hopRequestIssuesBefore = m_guiHopRequestIssues;"));
    const int advance = ready.indexOf(QStringLiteral("timerFrameEvent( predictiveGpuTexNrEarlyAdvance );"));
    const int issued = ready.indexOf(QStringLiteral("&& m_guiHopRequestIssues != hopRequestIssuesBefore"));
    const int add = ready.indexOf(QStringLiteral(
        "m_guiHopEmitToRequestIssue.add(\n                    ( m_guiHopLastRequestIssueStageTime - readyFrame.frameReadyEmitStageTime ) * 1000.0 );"));
    ASSERT_TRUE(before >= 0);
    ASSERT_TRUE(advance > before);
    ASSERT_TRUE(issued > advance);
    ASSERT_TRUE(add > issued);

    // Viewport paint: the shared paint sink's listener, set with the session and cleared where it ends.
    const QString sink = readRepoFile(QStringLiteral("platform/qt/PreviewPaintTiming.h"));
    ASSERT_TRUE(sink.contains(QStringLiteral("if( heard ) m_listener.fn( m_listener.context, m_listenerStartS, endS );")));
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("preview_paint_timing::paintListener().now = &guiHopWaitClockNow;")));
    const QString begin = beginSmokeBody();
    const int listen = begin.indexOf(QStringLiteral("preview_paint_timing::paintListener().fn = &guiHopWaitNoteViewportPaint;"));
    ASSERT_TRUE(listen >= 0);
    ASSERT_TRUE(begin.indexOf(QStringLiteral("m_guiHopWait.reset();")) < listen);
    ASSERT_TRUE(begin.indexOf(QStringLiteral("m_guiHopEmitToRequestIssue.reset();")) < listen);
    // Set inside the synchronous begin (no event is processed in it, so nothing feeds the split before it).
    ASSERT_TRUE(begin.indexOf(QStringLiteral("m_playbackSmokeActive = true;")) < listen);
    ASSERT_TRUE(listen < begin.indexOf(QStringLiteral("m_playbackSmokeStartTime = mlv_stage_timing_now();")));
    const QString finish = finishSmokeBody();
    const QString endAnchor = QStringLiteral("    m_playbackSmokeActive = false;\n    m_playbackPresentTrace.end();\n");
    const int inactive = finish.indexOf(endAnchor);
    ASSERT_TRUE(inactive >= 0);
    ASSERT_EQ(inactive + static_cast<int>(endAnchor.size()) + 4,
              finish.indexOf(QStringLiteral("preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();")));
    ASSERT_TRUE(bracedBlockAfter(source, QStringLiteral("MainWindow::~MainWindow()"))
                    .contains(QStringLiteral("preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();")));

    // The listener reaches the sink only from an outermost Scope; none is called with no listener set.
    preview_paint_timing::reset();
    static int calls = 0;
    static double clockS = 0.0;
    static double heardStartS = -1.0;
    static double heardEndS = -1.0;
    preview_paint_timing::paintListener().fn = [](void *, double startS, double endS) { ++calls; heardStartS = startS; heardEndS = endS; };
    preview_paint_timing::paintListener().now = []() -> double { clockS += 0.001; return clockS; };
    {
        const preview_paint_timing::Scope outer;
        const preview_paint_timing::Scope inner;
    }
    ASSERT_EQ(1, calls);
    ASSERT_NEAR(0.001, heardStartS, 1e-12);   // the outer Scope's two clock reads only
    ASSERT_NEAR(0.002, heardEndS, 1e-12);
    preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();
    {
        const preview_paint_timing::Scope unheard;
    }
    ASSERT_EQ(1, calls);
    preview_paint_timing::reset();
}

TEST(CriticalHopSummaryWiring, AdvA1A2EmitToRequestEndsAtTheRequestsOwnIssueStamp)
{
    // r1 adversary A1 on 6a6d7ef4: emit 100.000, advance_start 100.005, the request handed to the render thread at
    // 100.025 -> 25 ms, not the 5 ms advance_start gave. A2: a CUDA early advance whose next frame was already covered
    // by an active lookahead request advances the serial (drawFrame takes it) and returns without issuing; the serial
    // inequality counted it. The sample now comes from drawFrame's own stamp at the renderFrame hand-off.
    const QString source = mainWindowSource();
    const QString drawFrame = bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )"));
    ASSERT_FALSE(drawFrame.isEmpty());
    const int serial = drawFrame.indexOf(QStringLiteral("const uint64_t requestSerial = m_nextRenderRequestSerial++;"));
    const int covered = drawFrame.indexOf(QStringLiteral("QTimer::singleShot( 0, this, &MainWindow::drawFrameReady );"));
    // r2 (adversary A2 on e447bc14): the stamp follows each branch's renderFrame hand-off (AdvA2IssueStamp... below).
    const int stamp = drawFrame.indexOf(QStringLiteral(
        "        if( m_playbackSmokeActive )\n        {\n            m_guiHopLastRequestIssueStageTime = mlv_stage_timing_now();\n"
        "            ++m_guiHopRequestIssues;\n        }"));
    const int firstIssue = drawFrame.indexOf(QStringLiteral("m_pRenderThread->renderFrame( requestedFrame,"));
    ASSERT_TRUE(serial >= 0);
    ASSERT_TRUE(covered > serial);   // the covered-by-lookahead return is before the stamp
    ASSERT_TRUE(firstIssue > covered);
    ASSERT_TRUE(stamp > firstIssue);
    ASSERT_EQ(2, drawFrame.mid(covered).count(QStringLiteral("m_pRenderThread->renderFrame( requestedFrame,")));
    ASSERT_EQ(2, source.count(QStringLiteral("++m_guiHopRequestIssues;")));
    const QString ready = bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(ready.contains(QStringLiteral("hopRequestSerialBefore")));
    ASSERT_FALSE(ready.contains(QStringLiteral("m_guiHopEmitToRequestIssue.add( ( advance_start")));
    ASSERT_TRUE(beginSmokeBody().contains(QStringLiteral("m_guiHopRequestIssues = 0;")));

    // The arithmetic the early advance applies to the A1 input.
    gui_hop_wait::MeanMs hop;
    const double emitS = 100.000, issueS = 100.025;
    hop.add((issueS - emitS) * 1000.0);
    ASSERT_NEAR(25.0, hop.avg(), 1e-6);
}

TEST(CriticalHopSummaryWiring, AdvA3APaintDescheduledAfterItEndsKeepsItsOwnInterval)
{
    // r1 adversary A3 on 6a6d7ef4: a paint over [100.000, 100.010] (10 ms), the GUI thread descheduled after the paint
    // and the listener's clock read at 100.510 gave [100.500, 100.510]: 10 ms paint / 505 ms other in the wait
    // [100.005, 100.520] instead of 5 / 510. The Scope now reads the listener's clock at its own start and as the first
    // thing at its end, so the interval is the paint's.
    preview_paint_timing::reset();
    static gui_hop_wait::Recorder recorder;
    static int reads = 0;
    recorder.reset();
    reads = 0;
    preview_paint_timing::paintListener().fn = [](void *context, double startS, double endS) {
        static_cast<gui_hop_wait::Recorder *>(context)->push(gui_hop_wait::Kind::ViewportPaint, startS, endS);
    };
    preview_paint_timing::paintListener().now = []() -> double { return ++reads == 1 ? 100.000 : 100.010; };
    preview_paint_timing::paintListener().context = &recorder;
    {
        const preview_paint_timing::Scope paint;
    }
    preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();
    ASSERT_EQ(2, reads);
    ASSERT_EQ(1, recorder.ring(gui_hop_wait::Kind::ViewportPaint).count());
    const gui_hop_wait::Split s = recorder.split(100.005, 100.520);
    ASSERT_NEAR(5.0, s.kindMs[static_cast<int>(gui_hop_wait::Kind::ViewportPaint)], 1e-6);
    ASSERT_NEAR(510.0, s.otherMs, 1e-6);
    // A listener that changed inside the paint (a session ended or began) is not told.
    reads = 0;
    preview_paint_timing::paintListener().fn = [](void *context, double startS, double endS) {
        static_cast<gui_hop_wait::Recorder *>(context)->push(gui_hop_wait::Kind::ViewportPaint, startS, endS);
    };
    preview_paint_timing::paintListener().now = []() -> double { return ++reads == 1 ? 200.000 : 200.010; };
    preview_paint_timing::paintListener().context = &recorder;
    {
        const preview_paint_timing::Scope paint;
        preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();
    }
    ASSERT_EQ(1, recorder.ring(gui_hop_wait::Kind::ViewportPaint).count());
    preview_paint_timing::reset();
    // The MainWindow listener pushes what it is told, and the session sets the clock with it.
    const QString source = mainWindowSource();
    const QString note = bracedBlockAfter(source, QStringLiteral("static void guiHopWaitNoteViewportPaint( void *context, double startS, double endS )"));
    ASSERT_TRUE(note.contains(QStringLiteral("push( gui_hop_wait::Kind::ViewportPaint, startS, endS );")));
    ASSERT_FALSE(note.contains(QStringLiteral("mlv_stage_timing_now()")));
}

TEST(CriticalHopSummaryWiring, AdvA2PrepResultWaitCountsAnEarlierPresentStillRunning)
{
    // r1 adversary A2 on df1b864e: prep_result_wait was split by runner sleep and viewport paint only, so an earlier
    // present [100.000, 100.050] running when this result became ready (100.010) was charged to other: the present
    // entered at 100.060 had 50 ms other. It is 40 ms prepared_present + 10 ms other, and the line reports it.
    gui_hop_wait::Recorder recorder;
    recorder.push(gui_hop_wait::Kind::PreparedPresent, 100.000, 100.050);
    const gui_hop_wait::Split s = recorder.split(100.010, 100.060);
    ASSERT_NEAR(40.0, s.kindMs[static_cast<int>(gui_hop_wait::Kind::PreparedPresent)], 1e-6);
    ASSERT_NEAR(10.0, s.otherMs, 1e-6);
    const QString present = presentPreparedBody();
    ASSERT_FALSE(present.contains(QStringLiteral("kindBit")));
    const QString finish = finishSmokeBody();
    // CPU-S1-GUI-HOP-SLEEP-AND-PRESENT-1 appends five fields after it, so it is no longer the line's last argument
    // (TheFiveNewFieldsAreTheLastFiveOfCriticalHopSummary pins what follows it).
    ASSERT_EQ(1, finish.count(QStringLiteral(".arg( pr.avgKindMs( Kind::PreparedPresent ), 0, 'f', 3 )")));
}

TEST(CriticalHopSummaryWiring, AdvA3BAListenerReinstalledInsideAPaintIsNotTold)
{
    // r1 advisory A3 on df1b864e: a session that ended and a new one that began inside one paint re-install the same
    // fn/now/context tuple, so the paint (started under the old session) was told. Each installation has a generation.
    preview_paint_timing::reset();
    static int calls = 0;
    calls = 0;
    const auto fn = [](void *, double, double) { ++calls; };
    const auto now = []() -> double { return 1.0; };
    preview_paint_timing::paintListener().fn = fn;
    preview_paint_timing::paintListener().now = now;
    preview_paint_timing::paintListener().generation = preview_paint_timing::nextPaintListenerGeneration();
    {
        const preview_paint_timing::Scope paint;
        preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();
        preview_paint_timing::paintListener().fn = fn;
        preview_paint_timing::paintListener().now = now;
        preview_paint_timing::paintListener().generation = preview_paint_timing::nextPaintListenerGeneration();
    }
    ASSERT_EQ(0, calls);
    {
        const preview_paint_timing::Scope paint;   // the same installation throughout: told
    }
    ASSERT_EQ(1, calls);
    preview_paint_timing::paintListener() = preview_paint_timing::PaintListener();
    preview_paint_timing::reset();
    const QString begin = beginSmokeBody();
    const int context = begin.indexOf(QStringLiteral("preview_paint_timing::paintListener().context = &m_guiHopWait;"));
    const int generation = begin.indexOf(QStringLiteral(
        "preview_paint_timing::paintListener().generation = preview_paint_timing::nextPaintListenerGeneration();"));
    ASSERT_TRUE(context >= 0);
    ASSERT_TRUE(generation > context);
}

// ---- #813 salvage (CPU-S1-PACE-POST-785-LEVER-1, 8c632800): max_gui_busy_at_ms / max_gui_busy_frame -------------

TEST(GuiPresentAttributionWiring, MaxBusyGapFieldsAreAppendedAfterTheUnattributedSplit)
{
    const QString finish = finishSmokeBody();
    ASSERT_FALSE(finish.isEmpty());
    const int unattributed = finish.indexOf(QStringLiteral("QStringLiteral( \" avg_gui_other_busy_unattributed_ms=%1\" )"));
    const int freeze = finish.indexOf(QStringLiteral("QStringLiteral( \" max_gui_busy_at_ms=%1 max_gui_busy_frame=%2\" )"));
    const int format = finish.indexOf(QStringLiteral("\"playback_smoke.gui_present_summary session=%1 frames=%2 \""));
    const int appended = finish.indexOf(QStringLiteral("+ otherBusySplit;"));
    ASSERT_TRUE(unattributed > 0);
    ASSERT_TRUE(freeze > unattributed);   // after avg_gui_other_busy_unattributed_ms, the last field before it
    ASSERT_TRUE(format > freeze);         // the builder precedes the format, which appends it at the end
    ASSERT_TRUE(appended > format);
    for (const char *field : { "max_gui_busy_at_ms=%", "max_gui_busy_frame=%" })
        ASSERT_EQ(1, finish.count(QLatin1String(field)));
    // The lever's bucket field is not part of this salvage.
    ASSERT_FALSE(finish.contains(QStringLiteral("max_gui_other_busy_bucket")));
    ASSERT_TRUE(finish.contains(QStringLiteral(".arg( gaps.busyMaxAtMs, 0, 'f', 3 )\n                              .arg( gaps.busyMaxFrame );")));
}

TEST(GuiPresentAttributionWiring, MaxBusyGapStartIsStampedAtOpenOnTheSmokeClock)
{
    // Adversary A1 on fb4d13fe (8c632800's history): smoke start 100.000 s, the gap opens at 101.000 s and closes at
    // 101.020 s, then the GUI thread is descheduled 500 ms before a second clock read at the close: start = 1500 ms,
    // outside the gap [1000, 1020]. The start is the opening instant, stamped once in openGuiPresentGap, on the smoke
    // clock itself (r2 fix iteration 1, adversary A1 on 482616a5: no separate baseline read).
    const QString mw = mainWindowSource();
    const QString open = bracedBlockAfter(mw, QStringLiteral("void MainWindow::openGuiPresentGap( void )"));
    const QString close = closeGapBody();
    ASSERT_FALSE(open.isEmpty());
    ASSERT_FALSE(close.isEmpty());
    const int armed = open.indexOf(QStringLiteral("if( !m_playbackSmokeActive || !m_guiPresentIdleProbeConnected ) return;"));
    const int stamp = open.indexOf(QStringLiteral(
        "m_guiPresentGapStartSinceSmokeMs = static_cast<double>( gapOpenNs ) / 1000000.0 - m_playbackSmokeStartTime * 1000.0;"));
    const int start = open.indexOf(QStringLiteral("m_guiPresentGapStartNs = gapOpenNs;"));
    ASSERT_EQ(1, open.count(QStringLiteral("m_guiPresentGapClock.nsecsElapsed()")));
    ASSERT_FALSE(open.contains(QStringLiteral("mlv_stage_timing_now()")));
    ASSERT_TRUE(armed > 0);
    ASSERT_TRUE(stamp > armed);
    ASSERT_TRUE(start > stamp);
    ASSERT_TRUE(open.contains(QStringLiteral("m_guiPresentGapFrame = m_lastDisplayedPlaybackFrame;")));
    ASSERT_TRUE(close.contains(QStringLiteral("if( gaps.gaps == 1 || split.busyMs > gaps.busyMaxMs )")));
    ASSERT_TRUE(close.contains(QStringLiteral("gaps.busyMaxAtMs = m_guiPresentGapStartSinceSmokeMs;")));
    ASSERT_TRUE(close.contains(QStringLiteral("gaps.busyMaxFrame = m_guiPresentGapFrame;")));
    ASSERT_TRUE(close.indexOf(QStringLiteral("gaps.busyMaxAtMs =")) < close.indexOf(QStringLiteral("gaps.busyMaxMs = qMax(")));
    ASSERT_FALSE(close.contains(QStringLiteral("mlv_stage_timing_now()")));
    const QString begin = beginSmokeBody();
    ASSERT_TRUE(begin.contains(QStringLiteral("m_playbackSmokeStartTime = mlv_stage_timing_now();")));
    ASSERT_FALSE(begin.contains(QStringLiteral("m_guiPresentGapClock")));
    const QString h = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(h.contains(QStringLiteral("GuiPresentGapClock m_guiPresentGapClock;")));
    ASSERT_FALSE(h.contains(QStringLiteral("QElapsedTimer m_guiPresentGapClock;")));
    const QString clock = bracedBlockAfter(mw, QStringLiteral("qint64 MainWindow::GuiPresentGapClock::nsecsElapsed( void ) const"));
    ASSERT_TRUE(clock.contains(QStringLiteral("return static_cast<qint64>( mlv_stage_timing_now() * 1000000000.0 );")));
}

// ---- CPU-S1-GUI-HOP-SLEEP-AND-PRESENT-1 ----------------------------------------------------------------

namespace
{

// The balanced { ... } block of the function whose definition starts at `signatureStart` (its
// parameter list may span lines): the first '{' after it. Empty when the signature is missing.
QString functionBlockFrom(const QString & source, const QString & signatureStart)
{
    const int at = source.indexOf(signatureStart);
    if (at < 0) return QString();
    const int open = source.indexOf(QLatin1Char('{'), at);
    if (open < 0) return QString();
    int depth = 0;
    for (int i = open; i < source.length(); ++i)
    {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open, i - open + 1);
    }
    return QString();
}

// runGuiPlaybackSmoke's measured `for( ;; )` (the one after measuredState is declared).
QString measuredSmokeLoop(const QString & source)
{
    const QString smoke = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    return bracedBlockAfter(smoke, QStringLiteral(
        "playback_frame_range::PlayStopState measuredState = playback_frame_range::PlayStopState::Continue;\n"
        "    for( ;; )"));
}

} // namespace

TEST(SmokeRunnerEventWait, MeasuredLoopWaitsForEventsNotASleep)
{
    // The measured loop's tail slept 10 ms (QThread::msleep through the accounting helper), so a frameReady
    // the render thread queued during it waited for the sleep to end (parent card: 7.3 ms of an 18.1 ms
    // frame_ready_wait). It now waits in the dispatcher, bounded, and returns on the first event.
    const QString source = mainWindowSource();
    const QString loop = measuredSmokeLoop(source);
    ASSERT_FALSE(loop.isEmpty());
    ASSERT_EQ(1, loop.count(QStringLiteral("waitGuiThreadForSmokeEvents( 10 );")));
    ASSERT_EQ(0, loop.count(QStringLiteral("sleepGuiThreadForSmokeRunner(")));
    ASSERT_EQ(0, loop.count(QStringLiteral("msleep(")));
    // ...as the loop's last statement, after the stop check (the one break stays first).
    ASSERT_TRUE(loop.endsWith(QStringLiteral("waitGuiThreadForSmokeEvents( 10 );\n    }")));
    ASSERT_EQ(1, loop.count(QStringLiteral("break;")));
    ASSERT_TRUE(loop.indexOf(QStringLiteral("break;")) < loop.indexOf(QStringLiteral("waitGuiThreadForSmokeEvents( 10 );")));

    // The rest of runGuiPlaybackSmoke still sleeps through the accounting helper, never raw.
    const QString smoke = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_EQ(0, smoke.count(QStringLiteral("QThread::msleep(")));
    ASSERT_TRUE(smoke.count(QStringLiteral("sleepGuiThreadForSmokeRunner(")) > 0);
    ASSERT_EQ(1, source.count(QStringLiteral("waitGuiThreadForSmokeEvents( 10 );")));

    // The helper: a bounded precise single-shot timer, then one dispatcher wait. No sleep, no RunnerSleep push.
    const QString helper = bracedBlockAfter(source, QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_TRUE(helper.contains(QStringLiteral("bound.setSingleShot( true );")));
    ASSERT_TRUE(helper.contains(QStringLiteral("bound.setTimerType( Qt::PreciseTimer );")));
    ASSERT_TRUE(helper.contains(QStringLiteral("bound.start( qMax( 0, maxMs ) );")));
    ASSERT_TRUE(helper.contains(QStringLiteral(
        "qApp->processEvents( QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents );")));
    ASSERT_TRUE(helper.indexOf(QStringLiteral("bound.start(")) < helper.indexOf(QStringLiteral("WaitForMoreEvents")));
    ASSERT_EQ(0, helper.count(QStringLiteral("msleep")));
    ASSERT_EQ(0, helper.count(QStringLiteral("Sleep(")));
    ASSERT_EQ(0, helper.count(QStringLiteral("m_guiHopWait.push(")));
    ASSERT_EQ(0, helper.count(QStringLiteral("m_guiPresentGapRunnerSleepNs")));
    ASSERT_EQ(0, helper.count(QStringLiteral("noteSmokeRunnerSleepIfActive")));
    // Waits and timeouts are counted for the smoke session only (r2 fix iteration 1: by closeSmokeEventWait, for the
    // session the wait began in; AdvA1FinalWaitEndedByTheSessionsFinishIsStillCounted).
    ASSERT_TRUE(helper.indexOf(QStringLiteral("closeSmokeEventWait( wait );")) > helper.indexOf(QStringLiteral("WaitForMoreEvents")));
    const QString close = bracedBlockAfter(source, QStringLiteral("void MainWindow::closeSmokeEventWait( SmokeEventWaitInFlight &wait )"));
    ASSERT_FALSE(close.isEmpty());
    const int counted = close.indexOf(QStringLiteral(
        "if( !m_playbackSmokeActive || wait.session == 0 || wait.session != m_playbackSmokeSessionId )\n        return;"));
    ASSERT_TRUE(counted > 0);
    ASSERT_TRUE(close.indexOf(QStringLiteral("++m_playbackSmokeRunnerEventWaits;")) > counted);
    // (The timeout classification itself: AdvA1TimerEndedWaitIsATimeoutEvenWhenItsTimerEventIsUndelivered.)
    ASSERT_TRUE(close.indexOf(QStringLiteral("++m_playbackSmokeRunnerEventWaitTimeouts;")) > counted);
    ASSERT_EQ(0, helper.count(QStringLiteral("++m_playbackSmokeRunnerEventWait")));
    const QString h = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const int sleepDecl = h.indexOf(QStringLiteral("void sleepGuiThreadForSmokeRunner( unsigned long ms );"));
    ASSERT_TRUE(sleepDecl > 0);
    ASSERT_EQ(sleepDecl + static_cast<int>(QStringLiteral("void sleepGuiThreadForSmokeRunner( unsigned long ms );\n    ").size()),
              h.indexOf(QStringLiteral("void waitGuiThreadForSmokeEvents( int maxMs );")));
    // Defined right after the sleep helper.
    const int sleepDef = source.indexOf(QStringLiteral("void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )"));
    const int waitDef = source.indexOf(QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_TRUE(sleepDef > 0);
    ASSERT_TRUE(waitDef > sleepDef);
    ASSERT_EQ(0, source.mid(sleepDef + 1, waitDef - sleepDef - 1).count(QStringLiteral("\nvoid MainWindow::")));
}

TEST(SmokeRunnerEventWait, Gate0OwnWindowIsSampledBeforeThePresentIsAccounted)
{
    // Gate 0: the legacy uiLat (avg_present_ui_signal_latency_ms) sums m_lastDrawFrameReadyQueueMs at present
    // accounting -- the LATEST drawFrameReady's window. On the prep route a later drawFrameReady can overwrite it
    // before this present. The own window is this present's displayStart minus its frame's emit stamp.
    const QString source = mainWindowSource();
    const QString finish = functionBlockFrom(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"));
    ASSERT_FALSE(finish.isEmpty());
    const int gate = finish.indexOf(QStringLiteral(
        "if( m_playbackSmokeActive\n     && displayStart > 0.0\n     && readyFrame.frameReadyEmitStageTime > 0.0\n"
        "     && displayStart >= readyFrame.frameReadyEmitStageTime )"));
    const int own = finish.indexOf(QStringLiteral(
        "const double ownWindowMs = ( displayStart - readyFrame.frameReadyEmitStageTime ) * 1000.0;"));
    const int sum = finish.indexOf(QStringLiteral("m_playbackSmokePresentUiSignalLatencyOwnWindowSumMs += ownWindowMs;"));
    const int frames = finish.indexOf(QStringLiteral("++m_playbackSmokePresentUiSignalLatencyOwnWindowFrames;"));
    const int overwritten = finish.indexOf(QStringLiteral(
        "++m_playbackSmokePresentUiSignalLatencyOverwrittenFrames;"));
    const int note = finish.indexOf(QStringLiteral("notePlaybackSmokePresentedFrame( displayFrame, readyFrame, requestContext );"));
    ASSERT_TRUE(gate > 0);
    ASSERT_TRUE(own > gate);
    ASSERT_TRUE(sum > own);
    ASSERT_TRUE(frames > own);
    ASSERT_TRUE(overwritten > own);
    ASSERT_TRUE(note > overwritten);
    ASSERT_EQ(1, finish.count(QStringLiteral("notePlaybackSmokePresentedFrame(")));
    // displayStart is drawFrameReady's display_start (carried on the prep task), the same stamp the legacy
    // window and the frame_ready_wait split end at.
    ASSERT_TRUE(source.contains(QStringLiteral("task.displayStart = display_start;")));
    // The legacy definition is untouched (the slip histogram reads it).
    ASSERT_TRUE(notePresentedFrameBody().contains(QStringLiteral(
        "const double presentUiSignalLatencyMs = m_lastDrawFrameReadyQueueMs;")));
    ASSERT_TRUE(notePresentedFrameBody().contains(QStringLiteral(
        "m_playbackSmokePresentUiSignalLatencySumMs += presentUiSignalLatencyMs;")));

    // Reset with the other critical-hop state, inside the synchronous begin.
    const QString begin = beginSmokeBody();
    const int hopReset = begin.indexOf(QStringLiteral("m_guiHopFrameReadyWait.reset();"));
    ASSERT_TRUE(hopReset > 0);
    for (const char * reset : { "m_playbackSmokePresentUiSignalLatencyOwnWindowSumMs = 0.0;",
                                "m_playbackSmokePresentUiSignalLatencyOwnWindowFrames = 0;",
                                "m_playbackSmokePresentUiSignalLatencyOverwrittenFrames = 0;",
                                "m_playbackSmokeRunnerEventWaits = 0;",
                                "m_playbackSmokeRunnerEventWaitTimeouts = 0;",
                                "m_playbackSmokeRunnerEventWaitOverruns = 0;" })
    {
        const int at = begin.indexOf(QString::fromLatin1(reset));
        if (at <= hopReset) ::minitest::fail(__FILE__, __LINE__, "Gate 0 reset beside the hop reset", reset);
    }
}

TEST(SmokeRunnerEventWait, TheFiveNewFieldsAreTheLastFiveOfCriticalHopSummary)
{
    const QString finish = finishSmokeBody();
    const QString format = summaryFormat(finish, QStringLiteral("playback_smoke.critical_hop_summary session=%1"));
    ASSERT_FALSE(format.isEmpty());
    QStringList keys;
    const QRegularExpression key(QStringLiteral("([a-z0-9_]+)=%(\\d+)"));
    for (QRegularExpressionMatchIterator it = key.globalMatch(format); it.hasNext();)
        keys << it.next().captured(1);
    // r2 (adversary A1 on e447bc14): a sixth field, runner_event_wait_overruns, is appended after the five.
    ASSERT_EQ(23, static_cast<int>(keys.size()));
    const QStringList tail = keys.mid(keys.size() - 6);
    const QStringList expected = {
        QStringLiteral("present_ui_signal_latency_own_window_frames"),
        QStringLiteral("avg_present_ui_signal_latency_own_window_ms"),
        QStringLiteral("present_ui_signal_latency_overwritten_frames"),
        QStringLiteral("runner_event_waits"),
        QStringLiteral("runner_event_wait_timeouts"),
        QStringLiteral("runner_event_wait_overruns"),
    };
    ASSERT_TRUE(tail == expected);
    ASSERT_TRUE(keys.at(16) == QStringLiteral("avg_prep_result_wait_prepared_present_ms"));
    // Their arguments follow the 17th's, in the same order; the average is over own-window frames only.
    const int lastOld = finish.indexOf(QStringLiteral(".arg( pr.avgKindMs( Kind::PreparedPresent ), 0, 'f', 3 )"));
    const int a18 = finish.indexOf(QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokePresentUiSignalLatencyOwnWindowFrames ) )"));
    const int a19 = finish.indexOf(QStringLiteral(
        "? m_playbackSmokePresentUiSignalLatencyOwnWindowSumMs\n"
        "                                   / static_cast<double>( m_playbackSmokePresentUiSignalLatencyOwnWindowFrames )"));
    const int a20 = finish.indexOf(QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokePresentUiSignalLatencyOverwrittenFrames ) )"));
    const int a21 = finish.indexOf(QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokeRunnerEventWaits ) )"));
    const int a22 = finish.indexOf(QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokeRunnerEventWaitTimeouts ) )"));
    const int a23 = finish.indexOf(QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokeRunnerEventWaitOverruns ) );"));
    ASSERT_TRUE(lastOld > 0);
    ASSERT_TRUE(a18 > lastOld);
    ASSERT_TRUE(a19 > a18);
    ASSERT_TRUE(a20 > a19);
    ASSERT_TRUE(a21 > a20);
    ASSERT_TRUE(a22 > a21);
    ASSERT_TRUE(a23 > a22);
}

TEST(SmokeRunnerEventWait, AdvA1BatchPostedBehindTheExpiredTimerIsCountedAsAnOverrun)
{
    // r2, adversary A1 on e447bc14 (DISTINGUISHED, not bounded): waitGuiThreadForSmokeEvents(10), timer armed at 0 ms,
    // the GUI resumes at 11 ms with the expired timer's QTimerEvent posted first and a 16 ms playbackPrepResultReady
    // callback posted second. On Qt 6.10.2/Windows both are delivered by one sendPostedEvents loop that checks no
    // interrupt or deadline, so the wait returns at ~27 ms. Before r2 nothing told that wait from an 11 ms timeout.
    using gui_hop_wait::smokeEventWaitOverran;
    using gui_hop_wait::smokeEventWaitTimedOut;
    const long long adversaryNs = 27000000LL;
    ASSERT_TRUE(smokeEventWaitOverran(adversaryNs, 10));
    ASSERT_TRUE(smokeEventWaitTimedOut(false, adversaryNs, 10));   // an overrun is also a timeout
    ASSERT_TRUE(smokeEventWaitTimedOut(true, adversaryNs, 10));
    ASSERT_TRUE(smokeEventWaitOverran(11000001LL, 10));            // past the bound plus the timer's 1 ms
    ASSERT_FALSE(smokeEventWaitOverran(11000000LL, 10));
    ASSERT_FALSE(smokeEventWaitOverran(10300000LL, 10));           // an ordinary timeout
    ASSERT_FALSE(smokeEventWaitOverran(400000LL, 10));             // an event inside the bound
    ASSERT_TRUE(smokeEventWaitOverran(1000001LL, 0));
    ASSERT_FALSE(smokeEventWaitOverran(1000000LL, -5));
    // Every overrun is a timeout, at any bound.
    for (int maxMs : { 0, 1, 2, 10, 33 })
        for (long long ns : { 0LL, 999999LL, 1000001LL, 9000000LL, 11000001LL, 34000001LL, 50000000LL })
            if (smokeEventWaitOverran(ns, maxMs) && !smokeEventWaitTimedOut(true, ns, maxMs))
                ::minitest::fail(__FILE__, __LINE__, "an overrun that is not a timeout", "");

    // r2 fix iteration 1 (advisory A2 on 9f2a3316): the bound plus 1 ms is taken in long long, so INT_MAX is defined.
    ASSERT_FALSE(smokeEventWaitOverran(0LL, 2147483647));
    ASSERT_FALSE(smokeEventWaitOverran(2147483648LL * 1000000LL, 2147483647));
    ASSERT_TRUE(smokeEventWaitOverran(2147483648LL * 1000000LL + 1LL, 2147483647));

    // Wiring: one elapsed read classifies both counters, after the wait (r2 fix iteration 1: in closeSmokeEventWait,
    // which the wait calls on return); the helper states the bound's real contract.
    const QString source = mainWindowSource();
    const QString helper = bracedBlockAfter(source, QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_FALSE(helper.isEmpty());
    const QString close = bracedBlockAfter(source, QStringLiteral("void MainWindow::closeSmokeEventWait( SmokeEventWaitInFlight &wait )"));
    ASSERT_FALSE(close.isEmpty());
    const int wait = helper.indexOf(QStringLiteral("qApp->processEvents( QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents );"));
    ASSERT_TRUE(wait > 0);
    ASSERT_TRUE(helper.indexOf(QStringLiteral("closeSmokeEventWait( wait );")) > wait);
    const int read = close.indexOf(QStringLiteral("const qint64 waitedNs = wait.clock.nsecsElapsed();"));
    const int timeout = close.indexOf(QStringLiteral(
        "if( gui_hop_wait::smokeEventWaitTimedOut( wait.bound->isActive(), waitedNs, wait.maxMs ) )\n"
        "        ++m_playbackSmokeRunnerEventWaitTimeouts;"));
    const int overrun = close.indexOf(QStringLiteral(
        "if( gui_hop_wait::smokeEventWaitOverran( waitedNs, wait.maxMs ) )\n"
        "        ++m_playbackSmokeRunnerEventWaitOverruns;"));
    ASSERT_TRUE(read > 0);
    ASSERT_TRUE(timeout > read);
    ASSERT_TRUE(overrun > timeout);
    ASSERT_EQ(1, close.count(QStringLiteral("nsecsElapsed()")));
    ASSERT_EQ(1, source.count(QStringLiteral("++m_playbackSmokeRunnerEventWaitOverruns;")));
    const int def = source.indexOf(QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    const QString contract = source.mid(def - 700, 700);
    ASSERT_TRUE(contract.contains(QStringLiteral("The bound's contract")));
    ASSERT_TRUE(contract.contains(QStringLiteral("counted as an overrun")));
    // No dispatcher interrupt(): it cannot split a posted batch, and it would reach any nested loop.
    ASSERT_EQ(0, helper.count(QStringLiteral("interrupt(")));
    const QString h = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(h.contains(QStringLiteral(
        "uint64_t m_playbackSmokeRunnerEventWaitTimeouts = 0;\n    uint64_t m_playbackSmokeRunnerEventWaitOverruns = 0;")));
}

TEST(SmokeRunnerEventWait, AdvA1FinalWaitEndedByTheSessionsFinishIsStillCounted)
{
    // r2 fix iteration 1, adversary A1 on 9f2a3316: --presented-frames 480; the final waitGuiThreadForSmokeEvents(10)
    // dispatches presentation 480, the target stop reaches finishPlaybackSmokeTelemetry INSIDE that dispatch at 24 ms,
    // the finish clears m_playbackSmokeActive and prints the counters, and the wait returns at 27 ms. The old
    // after-return `if( m_playbackSmokeActive )` then skipped it: the final wait, its timeout and its overrun were lost.
    // Now the wait is registered in flight before the dispatch and the finish closes it before the summary prints.
    using gui_hop_wait::smokeEventWaitOverran;
    using gui_hop_wait::smokeEventWaitTimedOut;
    const long long atFinishNs = 24000000LL;   // what the finish's close reads for the adversary's input
    ASSERT_TRUE(smokeEventWaitTimedOut(true, atFinishNs, 10));
    ASSERT_TRUE(smokeEventWaitOverran(atFinishNs, 10));

    const QString source = mainWindowSource();
    const QString helper = bracedBlockAfter(source, QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_FALSE(helper.isEmpty());
    // The wait belongs to the session active when it began (0 = none), and is registered BEFORE the dispatch.
    const int session = helper.indexOf(QStringLiteral("wait.session = m_playbackSmokeActive ? m_playbackSmokeSessionId : 0;"));
    const int armed = helper.indexOf(QStringLiteral("bound.start( qMax( 0, maxMs ) );"));
    const int registered = helper.indexOf(QStringLiteral("m_smokeEventWaitInFlight = &wait;"));
    const int dispatch = helper.indexOf(QStringLiteral("qApp->processEvents( QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents );"));
    const int ownClose = helper.indexOf(QStringLiteral("if( m_smokeEventWaitInFlight == &wait )\n        closeSmokeEventWait( wait );"));
    const int restored = helper.indexOf(QStringLiteral("m_smokeEventWaitInFlight = outer;"));
    ASSERT_TRUE(session > 0);
    ASSERT_TRUE(armed > session);
    ASSERT_TRUE(registered > armed);
    ASSERT_TRUE(dispatch > registered);
    ASSERT_TRUE(ownClose > dispatch);      // closed on return only if the finish did not close it already
    ASSERT_TRUE(restored > ownClose);
    ASSERT_EQ(0, helper.count(QStringLiteral("if( m_playbackSmokeActive )")));

    // The close: unregisters first (so it runs once), then counts only for the still-active session the wait began in.
    const QString close = bracedBlockAfter(source, QStringLiteral("void MainWindow::closeSmokeEventWait( SmokeEventWaitInFlight &wait )"));
    ASSERT_FALSE(close.isEmpty());
    const int unregister = close.indexOf(QStringLiteral("if( m_smokeEventWaitInFlight == &wait )\n        m_smokeEventWaitInFlight = nullptr;"));
    const int guard = close.indexOf(QStringLiteral(
        "if( !m_playbackSmokeActive || wait.session == 0 || wait.session != m_playbackSmokeSessionId )\n        return;"));
    const int waits = close.indexOf(QStringLiteral("++m_playbackSmokeRunnerEventWaits;"));
    ASSERT_TRUE(unregister >= 0);
    ASSERT_TRUE(guard > unregister);
    ASSERT_TRUE(waits > guard);
    ASSERT_EQ(1, source.count(QStringLiteral("++m_playbackSmokeRunnerEventWaits;")));

    // The finish closes an in-flight wait while the session is still active, before the summary that prints it.
    const QString finish = finishSmokeBody();
    const int finishClose = finish.indexOf(QStringLiteral(
        "if( m_smokeEventWaitInFlight )\n        closeSmokeEventWait( *m_smokeEventWaitInFlight );\n    m_playbackSmokeActive = false;"));
    const int summary = finish.indexOf(QStringLiteral("playback_smoke.critical_hop_summary session=%1"));
    ASSERT_TRUE(finishClose > 0);
    ASSERT_TRUE(summary > finishClose);
    ASSERT_EQ(1, finish.count(QStringLiteral("m_playbackSmokeActive = false;")));
    const QString h = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(h.contains(QStringLiteral("SmokeEventWaitInFlight *m_smokeEventWaitInFlight = nullptr;")));
    ASSERT_TRUE(h.contains(QStringLiteral(
        "void waitGuiThreadForSmokeEvents( int maxMs );\n    void closeSmokeEventWait( SmokeEventWaitInFlight &wait );")));
}

TEST(SmokeRunnerEventWait, AdvA1OverrunAfterTheFinishIsPrintedWhenTheWaitReturns)
{
    // r2 fix iteration 2, adversary A1 on fdb754e3: --presented-frames 480; the final wait's dispatch finishes the
    // session at waitedNs = 5 ms (timer active), so the finish's close records neither a timeout nor an overrun and
    // the summary prints that. The GUI thread is then descheduled 20 ms: the completed wait (>= 25 ms) overran, and
    // nothing ever said so. The summary cannot carry what happens after it prints; the wait now prints the completed
    // wait on its own line when it returns.
    using gui_hop_wait::smokeEventWaitOverran;
    using gui_hop_wait::smokeEventWaitTimedOut;
    ASSERT_FALSE(smokeEventWaitTimedOut(true, 5000000LL, 10));    // what the finish's close saw
    ASSERT_FALSE(smokeEventWaitOverran(5000000LL, 10));
    ASSERT_TRUE(smokeEventWaitTimedOut(true, 25000000LL, 10));    // the completed wait the new line classifies
    ASSERT_TRUE(smokeEventWaitOverran(25000000LL, 10));

    const QString source = mainWindowSource();
    const QString helper = bracedBlockAfter(source, QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_FALSE(helper.isEmpty());
    const int dispatch = helper.indexOf(QStringLiteral("qApp->processEvents( QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents );"));
    const int ownClose = helper.indexOf(QStringLiteral("if( m_smokeEventWaitInFlight == &wait )\n        closeSmokeEventWait( wait );"));
    const int afterFinish = helper.indexOf(QStringLiteral("else if( wait.counted )"));
    const int line = helper.indexOf(QStringLiteral(
        "\"playback_smoke.runner_event_wait_after_finish session=%1 waited_at_finish_ms=%2 \"\n"
        "                               \"waited_ms=%3 timed_out=%4 overran=%5\""));
    const int read = helper.indexOf(QStringLiteral("const qint64 waitedNs = wait.clock.nsecsElapsed();"));
    const int timedOut = helper.indexOf(QStringLiteral(".arg( gui_hop_wait::smokeEventWaitTimedOut( bound.isActive(), waitedNs, maxMs ) ? 1 : 0 )"));
    const int overran = helper.indexOf(QStringLiteral(".arg( gui_hop_wait::smokeEventWaitOverran( waitedNs, maxMs ) ? 1 : 0 );"));
    const int restored = helper.indexOf(QStringLiteral("m_smokeEventWaitInFlight = outer;"));
    ASSERT_TRUE(dispatch > 0);
    ASSERT_TRUE(ownClose > dispatch);
    ASSERT_TRUE(afterFinish > ownClose);   // only a wait the finish closed (closed on return -> the first branch)
    ASSERT_TRUE(read > afterFinish);       // the completed wait, read after the dispatch returned
    ASSERT_TRUE(line > read);
    ASSERT_TRUE(timedOut > line);
    ASSERT_TRUE(overran > timedOut);
    ASSERT_TRUE(restored > overran);
    ASSERT_TRUE(helper.contains(QStringLiteral(".arg( static_cast<qulonglong>( wait.session ) )")));
    ASSERT_TRUE(helper.contains(QStringLiteral(".arg( static_cast<double>( wait.countedAtNs ) / 1000000.0, 0, 'f', 3 )")));
    // The close marks the record counted, at the clock reading it classified, only after the session guard.
    const QString close = bracedBlockAfter(source, QStringLiteral("void MainWindow::closeSmokeEventWait( SmokeEventWaitInFlight &wait )"));
    const int guard = close.indexOf(QStringLiteral(
        "if( !m_playbackSmokeActive || wait.session == 0 || wait.session != m_playbackSmokeSessionId )\n        return;"));
    const int marked = close.indexOf(QStringLiteral("wait.counted = true;\n    wait.countedAtNs = waitedNs;"));
    ASSERT_TRUE(guard > 0);
    ASSERT_TRUE(marked > guard);
    ASSERT_EQ(1, source.count(QStringLiteral("playback_smoke.runner_event_wait_after_finish")));
    const QString h = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(h.contains(QStringLiteral("bool counted = false;")));
    ASSERT_TRUE(h.contains(QStringLiteral("qint64 countedAtNs = 0;")));
}

TEST(CriticalHopSummaryWiring, AdvA2IssueStampFollowsTheRenderFrameHandOffInBothBranches)
{
    // r2, adversary A2 on e447bc14: emit 100.000 s, issue stamp 100.005 s, the GUI descheduled before
    // queuePresentationRequest until 100.025 s, renderFrame called at 100.025 s. The stamp before the hand-off
    // gave 5 ms; stamped after renderFrame returns it is >= 25 ms, the hand-off included.
    const QString source = mainWindowSource();
    const QString drawFrame = bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )"));
    ASSERT_FALSE(drawFrame.isEmpty());
    const QString renderCall = QStringLiteral(
        "m_pRenderThread->renderFrame( requestedFrame,\n"
        "                                      renderOutputMode,\n"
        "                                      m_renderThreadUsingGpuBilinearDebayer,\n"
        "                                      m_renderThreadUsingGpuAmazeDebayer,\n"
        "                                      requestSerial,\n"
        "                                      requestContext,\n"
        "                                      presentationPreparation );\n");
    const QString stamp = QStringLiteral(
        "        if( m_playbackSmokeActive )\n        {\n            m_guiHopLastRequestIssueStageTime = mlv_stage_timing_now();\n"
        "            ++m_guiHopRequestIssues;\n        }\n");
    ASSERT_EQ(2, drawFrame.count(renderCall));
    ASSERT_EQ(2, drawFrame.count(renderCall + stamp));        // each hand-off is immediately followed by its stamp
    ASSERT_EQ(2, drawFrame.count(QStringLiteral("m_guiHopLastRequestIssueStageTime = mlv_stage_timing_now();")));
    // Nothing stamps before the hand-off any more.
    const int library = drawFrame.indexOf(QStringLiteral("//Get frame from library"));
    ASSERT_TRUE(library > 0);
    ASSERT_EQ(0, drawFrame.left(library).count(QStringLiteral("m_guiHopLastRequestIssueStageTime")));
    for (int at = drawFrame.indexOf(QStringLiteral("queuePresentationRequest( requestContext );")); at >= 0;
         at = drawFrame.indexOf(QStringLiteral("queuePresentationRequest( requestContext );"), at + 1))
    {
        const int call = drawFrame.indexOf(renderCall, at);
        const int stampAt = drawFrame.indexOf(stamp, at);
        if (!(call > at && stampAt > call)) ::minitest::fail(__FILE__, __LINE__, "stamp after hand-off", "");
    }
    // The consumer reads that stamp (MainWindow.cpp drawFrameReady early advance).
    ASSERT_TRUE(bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrameReady()")).contains(QStringLiteral(
        "( m_guiHopLastRequestIssueStageTime - readyFrame.frameReadyEmitStageTime ) * 1000.0 );")));
    gui_hop_wait::MeanMs hop;
    hop.add((100.025 - 100.000) * 1000.0);
    ASSERT_TRUE(hop.avg() >= 25.0 - 1e-6);
}

TEST(SmokeRunnerEventWait, AdvA1TimerEndedWaitIsATimeoutEvenWhenItsTimerEventIsUndelivered)
{
    // r1 adversary A1 on cf09566f: on Windows (Qt 6.10.2) the precise timer's callback POSTS its QTimerEvent and the
    // dispatcher can return on that wake-up before delivering it, so bound.isActive() is still true after a wait the
    // timer ended: waits=1, timeouts=0. The wait's own elapsed time decides too (the timer's 1 ms resolution allowed).
    using gui_hop_wait::smokeEventWaitTimedOut;
    // The adversary's input: maxMs 10, no event before 10 ms, the timer event posted but not delivered.
    ASSERT_TRUE(smokeEventWaitTimedOut(true, 10300000LL, 10));
    ASSERT_TRUE(smokeEventWaitTimedOut(true, 9200000LL, 10));     // a multimedia timer can fire under 1 ms early
    ASSERT_TRUE(smokeEventWaitTimedOut(false, 10100000LL, 10));   // the timer event was delivered
    ASSERT_TRUE(smokeEventWaitTimedOut(false, 400000LL, 10));     // delivered is a timeout whatever the clock says
    // An event (the queued frameReady) ended it well inside the bound.
    ASSERT_FALSE(smokeEventWaitTimedOut(true, 400000LL, 10));
    ASSERT_FALSE(smokeEventWaitTimedOut(true, 8900000LL, 10));
    ASSERT_FALSE(smokeEventWaitTimedOut(true, 0LL, 10));

    const QString source = mainWindowSource();
    const QString helper = bracedBlockAfter(source, QStringLiteral("void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )"));
    ASSERT_FALSE(helper.isEmpty());
    // r2 fix iteration 1 (adversary A1 on 9f2a3316): the clock is the in-flight record's (wait.clock), and the
    // classification is closeSmokeEventWait's, which the wait calls after its dispatch.
    const int started = helper.indexOf(QStringLiteral("wait.clock.start();"));
    const int wait = helper.indexOf(QStringLiteral("qApp->processEvents( QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents );"));
    const int classify = helper.indexOf(QStringLiteral("closeSmokeEventWait( wait );"));
    ASSERT_TRUE(bracedBlockAfter(source, QStringLiteral("void MainWindow::closeSmokeEventWait( SmokeEventWaitInFlight &wait )")).contains(
        QStringLiteral("if( gui_hop_wait::smokeEventWaitTimedOut( wait.bound->isActive(), waitedNs, wait.maxMs ) )\n"
                       "        ++m_playbackSmokeRunnerEventWaitTimeouts;")));
    ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).contains(
        QStringLiteral("QElapsedTimer clock;          // started BEFORE the bound is armed")));
    // r1 adversary A1 on b6adb0d2: the GUI thread descheduled between arming the timer (0 ns) and starting the clock
    // (9 ms), the timer expiring at 10.3 ms, reads waitedNs = 1.3 ms < the 9 ms bound: a timer-ended wait counted as
    // no timeout. The clock starts BEFORE the timer is armed, so waitedNs is at least the time since arming.
    const int armed = helper.indexOf(QStringLiteral("bound.start( qMax( 0, maxMs ) );"));
    ASSERT_TRUE(started >= 0);
    ASSERT_TRUE(armed > started);
    ASSERT_EQ(1, helper.count(QStringLiteral("wait.clock.start();")));
    ASSERT_EQ(1, helper.count(QStringLiteral("bound.start(")));
    ASSERT_FALSE(smokeEventWaitTimedOut(true, 1300000LL, 10));   // why the order matters: a late clock reads 1.3 ms
    ASSERT_TRUE(smokeEventWaitTimedOut(true, 10300000LL, 10));   // a clock started before arming reads >= 10.3 ms
    ASSERT_TRUE(armed < wait);
    ASSERT_TRUE(classify > wait);
    ASSERT_FALSE(helper.contains(QStringLiteral("if( !bound.isActive() )")));
}

TEST(SmokeRunnerEventWait, AdvA2EqualValuedOverwriteByAnotherRequestIsCounted)
{
    // r1 adversary A2 on cf09566f: request 101 (emit 100.0, displayStart 100.0078125) and request 102 (emit
    // 100.015625, displayStart 100.0234375) both queue 7.8125 ms; 102's drawFrameReady overwrites the member before
    // 101 is presented. The numeric difference is 0, so the overwrite was missed. The writer's request serial decides.
    using gui_hop_wait::presentUiLatencyOverwritten;
    const double ownA = (100.0078125 - 100.0) * 1000.0;
    const double legacyB = (100.0234375 - 100.015625) * 1000.0;
    ASSERT_NEAR(ownA, legacyB, 1e-9);
    ASSERT_TRUE(presentUiLatencyOverwritten(ownA, legacyB, 102u, 101u));
    ASSERT_FALSE(presentUiLatencyOverwritten(ownA, ownA, 101u, 101u));   // its own drawFrameReady wrote it last
    ASSERT_TRUE(presentUiLatencyOverwritten(7.8125, 9.0, 102u, 101u));
    ASSERT_TRUE(presentUiLatencyOverwritten(7.8125, 9.0, 101u, 101u));   // a different value is never its own

    const QString source = mainWindowSource();
    const QString ready = bracedBlockAfter(source, QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(ready.isEmpty());
    // Every write of the member names its writer: the reset at the top of the accounting, then the window itself.
    ASSERT_EQ(1, ready.count(QStringLiteral("m_lastDrawFrameReadyQueueMs = 0.0;")));
    ASSERT_TRUE(ready.contains(QStringLiteral(
        "m_lastDrawFrameReadyQueueMs = 0.0;\n    m_lastDrawFrameReadyQueueSerial = readyFrame.requestSerial;")));
    ASSERT_EQ(2, source.count(QStringLiteral("m_lastDrawFrameReadyQueueMs =")));
    ASSERT_EQ(1, source.count(QStringLiteral("m_lastDrawFrameReadyQueueSerial =")));
    const QString finish = functionBlockFrom(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"));
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "if( gui_hop_wait::presentUiLatencyOverwritten( ownWindowMs, m_lastDrawFrameReadyQueueMs,\n"
        "                                                     m_lastDrawFrameReadyQueueSerial, readyFrame.requestSerial ) )\n"
        "            ++m_playbackSmokePresentUiSignalLatencyOverwrittenFrames;")));
    ASSERT_FALSE(finish.contains(QStringLiteral("fabs( ownWindowMs - m_lastDrawFrameReadyQueueMs )")));
    ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h"))
                    .contains(QStringLiteral("uint64_t m_lastDrawFrameReadyQueueSerial = 0;")));
}
