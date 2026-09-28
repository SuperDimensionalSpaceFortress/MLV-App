// Wiring/census test: pins that --gui-smoke-playback's full-screen measured playback
// (CUDA-PERF-PLAYBACK-FULLSCREEN-1) (1) is entered/left ONLY from
// MainWindow::runGuiPlaybackSmoke(), never from normal (non-smoke) startup, (2) runs in
// the order foreground -> fullscreen -> foreground re-verify -> play trigger, (3) verifies
// full-screen geometry and logs the outcome, (4) is restored at session end via a scope
// guard that fires on every early-return path (not only the success path), (5) the
// actionFullscreen menu entry is not force-hidden (see CUDA-PLAYBACK-FULLSCREEN-UI-1's
// test_playback_fullscreen_ui_wiring.cpp for the normal-use coverage that replaced the old
// "stays hidden" pin), and (6) that the fullscreen/viewport telemetry reuses the existing
// MLVAPP_PLAYBACK_SMOKE_TELEMETRY gate rather than a new ad hoc flag.
// MainWindow.cpp/main.cpp need a full GUI build (not linked into console_tests), so this
// test reads the sources as text -- the call sites are pinned by markers, not by exercising
// a live window (mirrors test_playback_smoke_foreground_wiring.cpp's approach).
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

int countOccurrences(const QString & haystack, const QString & needle)
{
    int count = 0;
    int from = 0;
    while (true) {
        const int at = haystack.indexOf(needle, from);
        if (at < 0) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackSmokeFullscreenWiring, HeaderDeclaresTheFullscreenApi)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool enterPlaybackSmokeFullscreen( void );")));
    ASSERT_TRUE(header.contains(QStringLiteral("void leavePlaybackSmokeFullscreen( void );")));
    ASSERT_TRUE(header.contains(QStringLiteral("QSize playbackSmokeViewportSize( void ) const;")));
}

TEST(PlaybackSmokeFullscreenWiring, MenuActionIsNoLongerForceHidden)
{
    // CUDA-PLAYBACK-FULLSCREEN-UI-1 unhid full screen for normal use: the 2018
    // setVisible( false ) (and its "does not work well" comment) must be gone, and no
    // later setVisible( false ) may have been reintroduced on this action. See
    // test_playback_fullscreen_ui_wiring.cpp for the full visibility/shortcut/Esc/restore
    // coverage this round adds.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(source.contains(QStringLiteral("ui->actionFullscreen->setVisible( false )")));
}

TEST(PlaybackSmokeFullscreenWiring, EntryAndExitAreCalledExactlyOnceAndOnlyFromGuiPlaybackSmoke)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // enterPlaybackSmokeFullscreen(): exactly one call site (the definition itself uses
    // the qualified "MainWindow::enterPlaybackSmokeFullscreen" spelling, so this
    // unqualified search counts only the call site).
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("enterPlaybackSmokeFullscreen();")));
    // leavePlaybackSmokeFullscreen(): exactly one call site, from the guard destructor --
    // never called directly, so restoration cannot be skipped by a return that forgets it.
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("window->leavePlaybackSmokeFullscreen();")));

    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("enterPlaybackSmokeFullscreen();")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("PlaybackSmokeFullscreenGuard")));

    // The generic Play toggle handler (fires in BOTH smoke and normal GUI mode) must never
    // enter/leave full screen directly -- only runGuiPlaybackSmoke() may.
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("bool MainWindow::eventFilter"));
    const QString toggleSlice = toggleBody.isEmpty()
        ? source.mid(source.indexOf(QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)")), 4000)
        : toggleBody;
    ASSERT_FALSE(toggleSlice.isEmpty());
    ASSERT_FALSE(toggleSlice.contains(QStringLiteral("enterPlaybackSmokeFullscreen")));
    ASSERT_FALSE(toggleSlice.contains(QStringLiteral("leavePlaybackSmokeFullscreen")));
}

TEST(PlaybackSmokeFullscreenWiring, NormalGuiStartupNeverEntersFullscreen)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int normalGuiAt = source.indexOf(QStringLiteral("/* Normal GUI mode — unchanged */"));
    ASSERT_TRUE(normalGuiAt >= 0);
    const QString normalGuiTail = source.mid(normalGuiAt);
    ASSERT_FALSE(normalGuiTail.contains(QStringLiteral("enterPlaybackSmokeFullscreen")));
    ASSERT_TRUE(normalGuiTail.contains(QStringLiteral("w.show();")));
}

TEST(PlaybackSmokeFullscreenWiring, OrderIsForegroundThenFullscreenThenReverifiedForegroundThenPlay)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int targetFramesAt = smokeBody.indexOf(QStringLiteral("m_playbackSmokeTargetPresentedFrames ="));
    const int firstForegroundAt = smokeBody.indexOf(QStringLiteral("forcePlaybackSmokeWindowForeground();"), targetFramesAt);
    const int fullscreenAt = smokeBody.indexOf(QStringLiteral("enterPlaybackSmokeFullscreen();"), firstForegroundAt);
    const int secondForegroundAt = smokeBody.indexOf(
        QStringLiteral("forcePlaybackSmokeWindowForeground();"), fullscreenAt);
    const int triggerAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->trigger();"), secondForegroundAt);

    ASSERT_TRUE(targetFramesAt >= 0);
    ASSERT_TRUE(firstForegroundAt > targetFramesAt);
    ASSERT_TRUE(fullscreenAt > firstForegroundAt);
    ASSERT_TRUE(secondForegroundAt > fullscreenAt);
    ASSERT_TRUE(secondForegroundAt > firstForegroundAt); // distinct call sites, not the same match re-found
    ASSERT_TRUE(triggerAt > secondForegroundAt);
}

TEST(PlaybackSmokeFullscreenWiring, GuardRestoresOnEveryPathIncludingEarlyReturns)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "struct PlaybackSmokeFullscreenGuard\n"
        "    {\n"
        "        MainWindow *window;\n"
        "        ~PlaybackSmokeFullscreenGuard() { if( window ) window->leavePlaybackSmokeFullscreen(); }\n"
        "    } playbackSmokeFullscreenGuard{ this };")));

    // At least one early-return error path exists textually AFTER the guard is
    // constructed within runGuiPlaybackSmoke() -- proving there is something for the
    // guard's destructor to actually protect, not just the final success return.
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const int guardAt = smokeBody.indexOf(QStringLiteral("PlaybackSmokeFullscreenGuard"));
    ASSERT_TRUE(guardAt >= 0);
    const QString afterGuard = smokeBody.mid(guardAt);
    ASSERT_TRUE(countOccurrences(afterGuard, QStringLiteral("return 7;")) >= 1);
    ASSERT_TRUE(countOccurrences(afterGuard, QStringLiteral("return 0;")) >= 1);
}

TEST(PlaybackSmokeFullscreenWiring, EnterTriggersTheExistingActionAndVerifiesGeometryOnBothAxes)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::enterPlaybackSmokeFullscreen( void )"),
        QStringLiteral("void MainWindow::leavePlaybackSmokeFullscreen( void )"));
    ASSERT_FALSE(body.isEmpty());

    ASSERT_TRUE(body.contains(QStringLiteral("if( !ui->actionFullscreen->isChecked() )")));
    ASSERT_TRUE(body.contains(QStringLiteral("ui->actionFullscreen->trigger();")));
    // Main-window geometry check.
    ASSERT_TRUE(body.contains(QStringLiteral("mainVerified = isFullScreen() && screenSize.isValid() && size() == screenSize;")));
    // GPU display window/container geometry check, only when that path is active.
    ASSERT_TRUE(body.contains(QStringLiteral("if( GpuDisplayWindow::isActive() )")));
    ASSERT_TRUE(body.contains(QStringLiteral("gpuViewport = GpuDisplayWindow::displaySize();")));
    ASSERT_TRUE(body.contains(QStringLiteral("gpuVerified = screenSize.isValid() && gpuViewport == screenSize;")));
    // Bounded, not unbounded, wait for the resize to settle.
    ASSERT_TRUE(body.contains(QStringLiteral("for( int attempt = 0; attempt < 200; ++attempt )")));

    const int verifiedAt = body.indexOf(QStringLiteral("const bool verified = mainVerified && gpuVerified;"));
    const int logAt = body.indexOf(
        QStringLiteral("gui_smoke.fullscreen_request requested=1 verified=%1 screen=%2x%3 "));
    ASSERT_TRUE(verifiedAt >= 0);
    ASSERT_TRUE(logAt > verifiedAt);
    ASSERT_TRUE(body.contains(QStringLiteral("window=%4x%5 gpu_viewport=%6x%7 dpr=%8")));

    // Round 2 (CUDA-PLAYBACK-FULLSCREEN-UI-1): the caller now fails closed on this value,
    // so it must actually be returned, not just logged.
    const int returnAt = body.indexOf(QStringLiteral("return verified;"), logAt);
    ASSERT_TRUE(returnAt > logAt);
}

TEST(PlaybackSmokeFullscreenWiring, LeaveOnlyTogglesWhenStillCheckedAndMirrorsCtrlF)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::leavePlaybackSmokeFullscreen( void )"),
        QStringLiteral("QSize MainWindow::playbackSmokeViewportSize( void ) const"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("if( ui->actionFullscreen->isChecked() )")));
    ASSERT_TRUE(body.contains(QStringLiteral("ui->actionFullscreen->trigger();")));
}

TEST(PlaybackSmokeFullscreenWiring, ForcePlaybackSmokeWindowForegroundIsFullscreenSafe)
{
    // Mutation-check the specific guards that keep the second (post-fullscreen) call to
    // forcePlaybackSmokeWindowForeground() from silently undoing full screen.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::forcePlaybackSmokeWindowForeground( void )"),
        QStringLiteral("bool MainWindow::enterPlaybackSmokeFullscreen( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("const bool wasFullScreen = isFullScreen();")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( !wasFullScreen ) showNormal();")));
    ASSERT_TRUE(body.contains(QStringLiteral("ShowWindow( target, wasFullScreen ? SW_SHOW : SW_SHOWNORMAL );")));
}

// --- Fullscreen telemetry: gated on the existing MLVAPP_PLAYBACK_SMOKE_TELEMETRY flag,
// mirroring PlaybackSmokeForegroundWiring's "not a new ad hoc flag" pin.

TEST(PlaybackSmokeFullscreenWiring, SessionBeginSamplesFullscreenAndViewportGatedOnTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int beginAt = source.indexOf(QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"));
    ASSERT_TRUE(beginAt >= 0);
    const QString tail = source.mid(beginAt, 2500);
    ASSERT_TRUE(tail.contains(QStringLiteral(
        "m_playbackSmokeFullscreenAtBegin =\n"
        "        m_playbackSmokeFrameTelemetry && isFullScreen();")));
    ASSERT_TRUE(tail.contains(QStringLiteral("if( m_playbackSmokeFrameTelemetry )")));
    ASSERT_TRUE(tail.contains(QStringLiteral("playbackSmokeViewportSize();")));
}

TEST(PlaybackSmokeFullscreenWiring, GateResamplesFullscreenAndViewportFreshNotReusedFromBegin)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("const bool fullscreenAtGate = isFullScreen();")));
    ASSERT_TRUE(source.contains(QStringLiteral("const QSize viewportAtGate = playbackSmokeViewportSize();")));
}

TEST(PlaybackSmokeFullscreenWiring, ForegroundLineCarriesTheNewFullscreenAndViewportFields)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.foreground session=%1"));
    ASSERT_TRUE(lineAt >= 0);
    const QString tail = source.mid(lineAt, 1200);
    ASSERT_TRUE(tail.contains(QStringLiteral("fullscreen_at_begin=%6 fullscreen_at_gate=%7")));
    ASSERT_TRUE(tail.contains(QStringLiteral("viewport_at_begin=%8x%9 viewport_at_gate=%10x%11")));
}

// --- CUDA-PLAYBACK-FULLSCREEN-UI-1 round 2: fail-closed on unverified/lost full screen,
// and the measured-duration clock excludes the foreground+fullscreen preamble.

TEST(PlaybackSmokeFullscreenWiring, PlaybackClockStartsAfterTheFullscreenPreambleAndBeforeTheTrigger)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int fullscreenAt = smokeBody.indexOf(QStringLiteral("enterPlaybackSmokeFullscreen();"));
    const int secondForegroundAt = smokeBody.indexOf(
        QStringLiteral("forcePlaybackSmokeWindowForeground();"), fullscreenAt);
    const int failCheckAt = smokeBody.indexOf(
        QStringLiteral("if( !fullscreenVerified )"), secondForegroundAt);
    const int preambleMsAt = smokeBody.indexOf(
        QStringLiteral("const qint64 preambleMs = preambleClock.elapsed();"), failCheckAt);
    const int clockDeclAt = smokeBody.indexOf(
        QStringLiteral("QElapsedTimer playbackClock;"), preambleMsAt);
    const int clockStartAt = smokeBody.indexOf(QStringLiteral("playbackClock.start();"), clockDeclAt);
    const int triggerAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->trigger();"), clockStartAt);

    ASSERT_TRUE(fullscreenAt >= 0);
    ASSERT_TRUE(secondForegroundAt > fullscreenAt);
    ASSERT_TRUE(failCheckAt > secondForegroundAt);
    ASSERT_TRUE(preambleMsAt > failCheckAt);
    ASSERT_TRUE(clockDeclAt > preambleMsAt);
    ASSERT_TRUE(clockStartAt > clockDeclAt);
    ASSERT_TRUE(triggerAt > clockStartAt);
}

TEST(PlaybackSmokeFullscreenWiring, UnverifiedFullscreenFailsClosedBeforeTheTriggerWithADistinctExitCode)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("const bool fullscreenVerified = enterPlaybackSmokeFullscreen();")));

    const int checkAt = smokeBody.indexOf(QStringLiteral("if( !fullscreenVerified )"));
    ASSERT_TRUE(checkAt >= 0);
    const int failCallAt = smokeBody.indexOf(
        QStringLiteral("logFullscreenSmokeFailure( \"fullscreen_not_verified\" )"), checkAt);
    // Search for the trigger from the check onward -- an earlier, unrelated
    // ui->actionPlay->trigger() exists upstream (Look Assist auto-warmup settle).
    const int triggerAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->trigger();"), checkAt);
    ASSERT_TRUE(failCallAt > checkAt);
    ASSERT_TRUE(failCallAt < triggerAt);

    // The shared failure helper is the sole place "return 13" is spelled -- pin the code
    // there rather than at each call site, and pin its FAIL log shape.
    const int helperAt = smokeBody.indexOf(QStringLiteral("auto logFullscreenSmokeFailure = "));
    ASSERT_TRUE(helperAt >= 0);
    ASSERT_TRUE(helperAt < checkAt);
    const int helperReturnAt = smokeBody.indexOf(QStringLiteral("return 13;"), helperAt);
    ASSERT_TRUE(helperReturnAt > helperAt);
    ASSERT_TRUE(helperReturnAt < checkAt);
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "\"[GUI-SMOKE] FAIL reason=\" << reason << \" screen=\"")));
}

TEST(PlaybackSmokeFullscreenWiring, FullscreenLossMidSessionFailsClosedAfterTheDurationLoopBeforeStoppingPlay)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int loopAt = smokeBody.indexOf(
        QStringLiteral("while( playbackClock.elapsed() < durationMs && ui->actionPlay->isChecked() )"));
    ASSERT_TRUE(loopAt >= 0);
    const int playedMsAt = smokeBody.indexOf(
        QStringLiteral("const qint64 playedMs = playbackClock.elapsed();"), loopAt);
    ASSERT_TRUE(playedMsAt > loopAt);
    // CUDA-PLAYBACK-FULLSCREEN-UI-2: the gate now also fails on a nonzero lost count, not
    // just the final-state isFullScreen() check -- see the dedicated latch tests below.
    const int gateAt = smokeBody.indexOf(
        QStringLiteral("if( !isFullScreen() || m_playbackSmokeFullscreenLostCount > 0 )"), playedMsAt);
    ASSERT_TRUE(gateAt > playedMsAt);
    const int gateFailCallAt = smokeBody.indexOf(
        QStringLiteral("logFullscreenSmokeFailure( \"fullscreen_lost_mid_session\" )"), gateAt);
    ASSERT_TRUE(gateFailCallAt > gateAt);

    // Must run before play is stopped, so the guard is the only thing that ever touches
    // window state on this path (no early "stop, then decide" ordering to get wrong).
    const int stopPlayAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    ASSERT_TRUE(stopPlayAt > gateFailCallAt);
}

TEST(PlaybackSmokeFullscreenWiring, DoneLineReportsPreambleAndPlayedMsNextToRequestedDuration)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int doneAt = source.indexOf(QStringLiteral("\"[GUI-SMOKE] DONE clip=\""));
    ASSERT_TRUE(doneAt >= 0);
    const QString tail = source.mid(doneAt, 400);
    const int requestedFieldAt = tail.indexOf(QStringLiteral("duration_ms=\" << durationMs"));
    const int preambleAt = tail.indexOf(QStringLiteral("preamble_ms=\" << preambleMs"));
    const int playedAt = tail.indexOf(QStringLiteral("played_ms=\" << playedMs"));
    ASSERT_TRUE(requestedFieldAt >= 0);
    ASSERT_TRUE(preambleAt > requestedFieldAt);
    ASSERT_TRUE(playedAt > preambleAt);
}

// --- CUDA-PLAYBACK-FULLSCREEN-UI-2: the end-of-loop isFullScreen() check alone is a point
// sample and misses a lose-then-regain interval entirely within the measured window (e.g.
// Escape to windowed, then F11/Ctrl+F back to full screen before the loop ends). These pin
// the event-driven latch that fails closed on that case too, not just the final state.

TEST(PlaybackSmokeFullscreenWiring, HeaderDeclaresTheLossLatchApiAndState)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void changeEvent( QEvent *event );")));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_playbackSmokeFullscreenLossLatchArmed = false;")));
    ASSERT_TRUE(header.contains(QStringLiteral("uint64_t m_playbackSmokeFullscreenLostCount = 0;")));
}

TEST(PlaybackSmokeFullscreenWiring, ChangeEventOnlyCountsWindowStateChangesToNotFullscreenWhileArmed)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::changeEvent( QEvent *event )"),
        QStringLiteral("// Intercept FileOpen events"));
    ASSERT_FALSE(body.isEmpty());

    // Base class implementation must still run.
    ASSERT_TRUE(body.contains(QStringLiteral("QMainWindow::changeEvent( event );")));

    const int armedGateAt = body.indexOf(
        QStringLiteral("if( m_playbackSmokeFullscreenLossLatchArmed"));
    const int typeGateAt = body.indexOf(
        QStringLiteral("&& event->type() == QEvent::WindowStateChange"), armedGateAt);
    const int notFullscreenGateAt = body.indexOf(
        QStringLiteral("&& !isFullScreen() )"), typeGateAt);
    const int incrementAt = body.indexOf(
        QStringLiteral("++m_playbackSmokeFullscreenLostCount;"), notFullscreenGateAt);
    ASSERT_TRUE(armedGateAt >= 0);
    ASSERT_TRUE(typeGateAt > armedGateAt);
    ASSERT_TRUE(notFullscreenGateAt > typeGateAt);
    ASSERT_TRUE(incrementAt > notFullscreenGateAt);
}

TEST(PlaybackSmokeFullscreenWiring, LatchIsArmedOnlyAfterVerificationAndImmediatelyBeforeThePlayTrigger)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // Every windowState transition before verification (entry into full screen and its
    // settle-loop churn) must happen while the latch is still disarmed -- so the arm site
    // must be textually after the fail-closed verification check, not before it.
    const int failCheckAt = smokeBody.indexOf(QStringLiteral("if( !fullscreenVerified )"));
    const int clockStartAt = smokeBody.indexOf(QStringLiteral("playbackClock.start();"), failCheckAt);
    const int resetAt = smokeBody.indexOf(
        QStringLiteral("m_playbackSmokeFullscreenLostCount = 0;"), clockStartAt);
    const int armAt = smokeBody.indexOf(
        QStringLiteral("m_playbackSmokeFullscreenLossLatchArmed = true;"), resetAt);
    const int triggerAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->trigger();"), armAt);

    ASSERT_TRUE(failCheckAt >= 0);
    ASSERT_TRUE(clockStartAt > failCheckAt);
    ASSERT_TRUE(resetAt > clockStartAt);
    ASSERT_TRUE(armAt > resetAt);
    ASSERT_TRUE(triggerAt > armAt);
}

TEST(PlaybackSmokeFullscreenWiring, LatchIsDisarmedAtLoopExitBeforeTheGateCheckAndTeardown)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int loopAt = smokeBody.indexOf(
        QStringLiteral("while( playbackClock.elapsed() < durationMs && ui->actionPlay->isChecked() )"));
    const int playedMsAt = smokeBody.indexOf(
        QStringLiteral("const qint64 playedMs = playbackClock.elapsed();"), loopAt);
    const int disarmAt = smokeBody.indexOf(
        QStringLiteral("m_playbackSmokeFullscreenLossLatchArmed = false;"), playedMsAt);
    const int gateAt = smokeBody.indexOf(
        QStringLiteral("if( !isFullScreen() || m_playbackSmokeFullscreenLostCount > 0 )"), disarmAt);

    ASSERT_TRUE(loopAt >= 0);
    ASSERT_TRUE(playedMsAt > loopAt);
    ASSERT_TRUE(disarmAt > playedMsAt);
    ASSERT_TRUE(gateAt > disarmAt);

    // The guard's teardown (leavePlaybackSmokeFullscreen(), which itself toggles full
    // screen off) runs strictly after this disarm, on every path -- so restoring chrome at
    // session end is never itself counted.
    const int guardDefAt = smokeBody.indexOf(QStringLiteral("PlaybackSmokeFullscreenGuard"));
    ASSERT_TRUE(guardDefAt >= 0);
    ASSERT_TRUE(disarmAt > guardDefAt);
}

TEST(PlaybackSmokeFullscreenWiring, GateFailsClosedOnLostCountEvenWhenFinalStateIsFullscreen)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // The point-sample isFullScreen() check alone is no longer sufficient: a genuinely
    // full-screen final state with a nonzero lost count must still fail.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( !isFullScreen() || m_playbackSmokeFullscreenLostCount > 0 )")));
    const int gateAt = smokeBody.indexOf(
        QStringLiteral("if( !isFullScreen() || m_playbackSmokeFullscreenLostCount > 0 )"));
    const int failCallAt = smokeBody.indexOf(
        QStringLiteral("logFullscreenSmokeFailure( \"fullscreen_lost_mid_session\" )"), gateAt);
    ASSERT_TRUE(failCallAt > gateAt);
}

TEST(PlaybackSmokeFullscreenWiring, FailureHelperReportsTheLostCount)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("<< \" lost_count=\" << m_playbackSmokeFullscreenLostCount")));
}

TEST(PlaybackSmokeFullscreenWiring, ForegroundLineCarriesTheFullscreenLostCountField)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.foreground session=%1"));
    ASSERT_TRUE(lineAt >= 0);
    const QString tail = source.mid(lineAt, 1200);
    ASSERT_TRUE(tail.contains(QStringLiteral("fullscreen_lost_count=%12")));
    ASSERT_TRUE(tail.contains(
        QStringLiteral(".arg( static_cast<qulonglong>( m_playbackSmokeFullscreenLostCount ) );")));
}

TEST(PlaybackSmokeFullscreenWiring, HeaderNoLongerCallsTheActionMenuHidden)
{
    // sol NOTE (#168 round 1): the header comment still described actionFullscreen as
    // menu-hidden although CUDA-PLAYBACK-FULLSCREEN-UI-1 intentionally unhid it.
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const int declAt = header.indexOf(QStringLiteral("bool enterPlaybackSmokeFullscreen( void );"));
    ASSERT_TRUE(declAt >= 0);
    const int commentAt = header.lastIndexOf(QStringLiteral("// --gui-smoke-playback only"), declAt);
    ASSERT_TRUE(commentAt >= 0);
    const QString comment = header.mid(commentAt, declAt - commentAt);
    ASSERT_FALSE(comment.contains(QStringLiteral("menu-hidden")));
    ASSERT_TRUE(comment.contains(QStringLiteral("unhidden for normal use")));
}
