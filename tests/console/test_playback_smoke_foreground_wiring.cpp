// Wiring/census test: pins that --gui-smoke-playback's window-foreground request
// (CUDA-PERF-PLAYBACK-FOREGROUND-1) (1) is called ONLY from MainWindow::runGuiPlaybackSmoke(),
// never from the generic Play toggle or from normal (non-smoke) startup, (2) verifies with
// GetForegroundWindow() and logs the outcome, (3) restores normal (non-topmost) z-order
// rather than leaving the window permanently on top, and (4) that the foreground telemetry
// reuses the existing MLVAPP_PLAYBACK_SMOKE_TELEMETRY gate rather than a new ad hoc flag.
// MainWindow.cpp/main.cpp need a full GUI build (not linked into console_tests), so this test
// reads the sources as text -- the call sites are pinned by markers, not by exercising a live
// window (mirrors test_gpu_window_swap_wiring.cpp's approach for the same reason).
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

TEST(PlaybackSmokeForegroundWiring, HeaderDeclaresTheForegroundApi)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void forcePlaybackSmokeWindowForeground( void );")));
    ASSERT_TRUE(header.contains(QStringLiteral("void onPlaybackSmokeApplicationStateChanged( Qt::ApplicationState state );")));
}

TEST(PlaybackSmokeForegroundWiring, ForegroundRequestIsCalledOnlyFromGuiPlaybackSmoke)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // Exactly two call sites anywhere in the file (the definition itself uses the
    // qualified "MainWindow::forcePlaybackSmokeWindowForeground" spelling, so an
    // unqualified-name search below counts ONLY call sites, not the definition): once
    // before the full-screen switch, once after, to re-verify/re-establish foreground
    // once the window has re-laid-out (CUDA-PERF-PLAYBACK-FULLSCREEN-1).
    const int callCount = countOccurrences(source, QStringLiteral("forcePlaybackSmokeWindowForeground();"));
    ASSERT_EQ(2, callCount);

    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("forcePlaybackSmokeWindowForeground();")));

    // The generic Play toggle handler (fires in BOTH smoke and normal GUI mode) must never
    // call it directly -- only runGuiPlaybackSmoke() may, so normal interactive Play is
    // never affected.
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("bool MainWindow::eventFilter"));
    // on_actionPlay_toggled is defined after runGuiPlaybackSmoke in this file; if the
    // heuristic end-marker above ever goes stale, fall back to just checking absence within
    // a generously-sized slice starting at the toggle handler.
    const QString toggleSlice = toggleBody.isEmpty()
        ? source.mid(source.indexOf(QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)")), 4000)
        : toggleBody;
    ASSERT_FALSE(toggleSlice.isEmpty());
    ASSERT_FALSE(toggleSlice.contains(QStringLiteral("forcePlaybackSmokeWindowForeground")));
}

TEST(PlaybackSmokeForegroundWiring, ForegroundRequestRunsBeforeTheMeasuredPlayTrigger)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int forceAt = smokeBody.indexOf(QStringLiteral("forcePlaybackSmokeWindowForeground();"));
    const int targetFramesAt = smokeBody.indexOf(QStringLiteral("m_playbackSmokeTargetPresentedFrames ="));
    const int triggerAt = smokeBody.indexOf(QStringLiteral("programmaticPlay( \"gui-smoke-measured\""), targetFramesAt);
    ASSERT_TRUE(forceAt >= 0);
    ASSERT_TRUE(targetFramesAt >= 0);
    ASSERT_TRUE(triggerAt > targetFramesAt);
    ASSERT_TRUE(forceAt > targetFramesAt && forceAt < triggerAt);
}

TEST(PlaybackSmokeForegroundWiring, NormalGuiStartupNeverCallsTheForegroundRequest)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int normalGuiAt = source.indexOf(QStringLiteral("/* Normal GUI mode — unchanged */"));
    ASSERT_TRUE(normalGuiAt >= 0);
    const QString normalGuiTail = source.mid(normalGuiAt);
    ASSERT_FALSE(normalGuiTail.contains(QStringLiteral("forcePlaybackSmokeWindowForeground")));
    ASSERT_TRUE(normalGuiTail.contains(QStringLiteral("w.show();")));
}

TEST(PlaybackSmokeForegroundWiring, ForegroundRequestVerifiesAndLogsTheOutcome)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::forcePlaybackSmokeWindowForeground( void )"),
        QStringLiteral("void MainWindow::onPlaybackSmokeApplicationStateChanged( Qt::ApplicationState state )"));
    ASSERT_FALSE(body.isEmpty());

    ASSERT_TRUE(body.contains(QStringLiteral("SetForegroundWindow( target )")));
    const int verifiedAt = body.indexOf(QStringLiteral("const bool verified = target && GetForegroundWindow() == target;"));
    ASSERT_TRUE(verifiedAt >= 0);
    const int logAt = body.indexOf(QStringLiteral("gui_smoke.foreground_request requested=1 verified=%1"));
    ASSERT_TRUE(logAt > verifiedAt);
}

TEST(PlaybackSmokeForegroundWiring, ForegroundRequestDoesNotLeaveTheWindowPermanentlyTopmost)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::forcePlaybackSmokeWindowForeground( void )"),
        QStringLiteral("void MainWindow::onPlaybackSmokeApplicationStateChanged( Qt::ApplicationState state )"));
    ASSERT_FALSE(body.isEmpty());

    const int topmostAt = body.indexOf(QStringLiteral("SetWindowPos( target, HWND_TOPMOST,"));
    const int notopmostAt = body.indexOf(QStringLiteral("SetWindowPos( target, HWND_NOTOPMOST,"));
    ASSERT_TRUE(topmostAt >= 0);
    ASSERT_TRUE(notopmostAt > topmostAt);
}

TEST(PlaybackSmokeForegroundWiring, GpuDisplayWindowIsAlsoActivated)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::forcePlaybackSmokeWindowForeground( void )"),
        QStringLiteral("void MainWindow::onPlaybackSmokeApplicationStateChanged( Qt::ApplicationState state )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("GpuDisplayWindow::activeWindow()")));
    ASSERT_TRUE(body.contains(QStringLiteral("gpuWindow->requestActivate();")));
}

// --- Foreground telemetry: gated on the existing MLVAPP_PLAYBACK_SMOKE_TELEMETRY flag,
// mirroring GpuWindowSwapWiring's "not a new ad hoc flag" pin for swap telemetry.

TEST(PlaybackSmokeForegroundWiring, ApplicationStateConnectionIsGatedOnTheExistingSmokeTelemetryFlagAtConstruction)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if( playbackSmokeFrameTelemetryEnabled() )\n"
        "    {\n"
        "        connect( qApp, &QGuiApplication::applicationStateChanged,\n"
        "                 this, &MainWindow::onPlaybackSmokeApplicationStateChanged );\n"
        "    }")));
}

TEST(PlaybackSmokeForegroundWiring, ForegroundLostCounterOnlyCountsDuringAnOpenSession)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::onPlaybackSmokeApplicationStateChanged( Qt::ApplicationState state )"),
        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"));
    ASSERT_FALSE(body.isEmpty());
    const int activeGateAt = body.indexOf(QStringLiteral("if( !m_playbackSmokeActive ) return;"));
    const int activeStateGateAt = body.indexOf(QStringLiteral("if( state == Qt::ApplicationActive ) return;"));
    const int incrementAt = body.indexOf(QStringLiteral("++m_playbackSmokeForegroundLostCount;"));
    ASSERT_TRUE(activeGateAt >= 0);
    ASSERT_TRUE(activeStateGateAt > activeGateAt);
    ASSERT_TRUE(incrementAt > activeStateGateAt);
}

TEST(PlaybackSmokeForegroundWiring, SessionBeginResetsTheCounterAndSamplesForegroundAtBegin)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    const int beginAt = source.indexOf(QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"));
    ASSERT_TRUE(beginAt >= 0);
    const QString tail = source.mid(beginAt, 2500);
    const int resetAt = tail.indexOf(QStringLiteral("m_playbackSmokeForegroundLostCount = 0;"));
    const int sampleAt = tail.indexOf(QStringLiteral(
        "m_playbackSmokeForegroundAtBegin =\n"
        "        m_playbackSmokeFrameTelemetry && nativeWindowIsForeground( this );"));
    ASSERT_TRUE(resetAt >= 0);
    ASSERT_TRUE(sampleAt > resetAt);
}

TEST(PlaybackSmokeForegroundWiring, GateEmitsAForegroundSummaryLineGatedOnTelemetryEnabled)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int gateLineAt = source.indexOf(QStringLiteral("playback_smoke.gate session=%1"));
    const int foregroundGateAt = source.indexOf(QStringLiteral("if ( m_playbackSmokeFrameTelemetry )"), gateLineAt);
    const int foregroundLineAt = source.indexOf(QStringLiteral("playback_smoke.foreground session=%1"), gateLineAt);
    ASSERT_TRUE(gateLineAt >= 0);
    ASSERT_TRUE(foregroundGateAt > gateLineAt);
    ASSERT_TRUE(foregroundLineAt > foregroundGateAt);
}

TEST(PlaybackSmokeForegroundWiring, ForegroundAtGateIsSampledFreshNotReusedFromBegin)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const bool foregroundAtGate = nativeWindowIsForeground( this );")));
}

// Round 2 (CUDA-PLAYBACK-PRESENT-CADENCE-1): counters-only mode. The per-frame
// "playback_smoke.frame" and "playback_auto.decision" qInfo() lines are gated on a
// separate member (m_playbackSmokeFrameLogEnabled), derived from -- and never more
// permissive than -- m_playbackSmokeFrameTelemetry, so every counter this session
// accumulates keeps running when the new opt-out flag suppresses only the per-frame log
// lines.

TEST(PlaybackSmokeForegroundWiring, FrameLogEnabledIsDerivedFromFrameTelemetryAndTheNewOptOutFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int helperAt = source.indexOf(QStringLiteral(
        "static bool playbackSmokeFrameLogDisabledByEnvironment()"));
    ASSERT_TRUE(helperAt >= 0);
    ASSERT_TRUE(source.mid(helperAt, 200).contains(QStringLiteral(
        "environmentFlagEnabled( \"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG\" );")));

    const int beginAt = source.indexOf(QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"));
    ASSERT_TRUE(beginAt >= 0);
    const int assignAt = source.indexOf(QStringLiteral(
        "m_playbackSmokeFrameLogEnabled =\n"
        "        m_playbackSmokeFrameTelemetry && !playbackSmokeFrameLogDisabledByEnvironment();"),
        beginAt);
    ASSERT_TRUE(assignAt > beginAt);
    ASSERT_TRUE(assignAt < beginAt + 2500);
}

TEST(PlaybackSmokeForegroundWiring, PlaybackSmokeFrameLineIsGatedOnFrameLogEnabledNotFrameTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int gateAt = source.indexOf(QStringLiteral("if( m_playbackSmokeFrameLogEnabled )"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.frame session=%1"), gateAt);
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(lineAt > gateAt);
}

TEST(PlaybackSmokeForegroundWiring, PlaybackAutoDecisionLineIsGatedOnFrameLogEnabledNotFrameTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int firstGateAt = source.indexOf(QStringLiteral("if( m_playbackSmokeFrameLogEnabled )"));
    ASSERT_TRUE(firstGateAt >= 0);
    const int secondGateAt = source.indexOf(
        QStringLiteral("if( m_playbackSmokeFrameLogEnabled )"), firstGateAt + 1);
    ASSERT_TRUE(secondGateAt > firstGateAt);
    const int lineAt = source.indexOf(QStringLiteral("playback_auto.decision session=%1"), secondGateAt);
    ASSERT_TRUE(lineAt > secondGateAt);
}

TEST(PlaybackSmokeForegroundWiring, EligibilityDiagLineIsDedupedAgainstItsLastEmittedValue)
{
    // CUDA-PLAYBACK-PRESENT-CADENCE-1 round 2: this line used to run unconditionally on
    // every presented frame under MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1. Deduped
    // against the last emitted line rather than dropped, so the job-side
    // Get-AttrCudaLastEligibilityLine parse (which needs at least one occurrence) still
    // finds a correct verdict while a stable session logs it only once.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int diagGateAt = source.indexOf(QStringLiteral(
        "if( playback_recon_eligibility_diag_requested_by_environment() )"));
    ASSERT_TRUE(diagGateAt >= 0);
    const int eligibilityLineAt = source.indexOf(
        QStringLiteral("const QString eligibilityLine ="), diagGateAt);
    ASSERT_TRUE(eligibilityLineAt > diagGateAt);
    const int staticCacheAt = source.indexOf(
        QStringLiteral("static QString lastEligibilityLine;"), eligibilityLineAt);
    ASSERT_TRUE(staticCacheAt > eligibilityLineAt);
    const int compareAt = source.indexOf(
        QStringLiteral("if( eligibilityLine != lastEligibilityLine )"), staticCacheAt);
    ASSERT_TRUE(compareAt > staticCacheAt);
    const int logAt = source.indexOf(
        QStringLiteral("qInfo().noquote() << eligibilityLine;"), compareAt);
    ASSERT_TRUE(logAt > compareAt);
    const int updateAt = source.indexOf(
        QStringLiteral("lastEligibilityLine = eligibilityLine;"), logAt);
    ASSERT_TRUE(updateAt > logAt);
}
