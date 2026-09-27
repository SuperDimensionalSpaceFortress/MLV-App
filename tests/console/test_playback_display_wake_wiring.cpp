// Wiring/census test: pins that CUDA-PERF-DISPLAY-WAKE-1's display-wake request (1) is set once
// on play start and released once on stop AND on window close, never per frame, (2) is Windows-
// only (SetThreadExecutionState under Q_OS_WIN, a no-op elsewhere) and never gated on any
// telemetry flag, and (3) that beginPlaybackSmokeTelemetry() logs playback_smoke.display_required
// once per playback session. CUDA-PERF-DISPLAY-WAKE-2 extends this: (4) the acquisition outcome
// (not just the request) is captured and reported, and (5) a Windows-only nativeEvent() refuses
// the screen-saver/monitor-off WM_SYSCOMMAND while (and only while) playback holds the display
// required, counting refusals into a per-session counter that playback_smoke.summary reports.
// MainWindow.cpp needs a full GUI build (not linked into console_tests), so this test reads the
// source as text -- the call sites are pinned by markers, not by exercising a live window
// (mirrors test_playback_smoke_foreground_wiring.cpp's approach for the same reason).
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

TEST(PlaybackDisplayWakeWiring, HelperIsWindowsOnlyAndUsesTheRightFlags)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("static bool setPlaybackDisplayRequiredExecutionState( bool required )"),
        QStringLiteral("/* spaceTag argument options"));
    ASSERT_FALSE(body.isEmpty());

    const int ifdefAt = body.indexOf(QStringLiteral("#ifdef Q_OS_WIN"));
    const int callAt = body.indexOf(QStringLiteral(
        "SetThreadExecutionState( required ? ( ES_CONTINUOUS | ES_DISPLAY_REQUIRED ) : ES_CONTINUOUS );"));
    const int elseAt = body.indexOf(QStringLiteral("#else"), callAt);
    ASSERT_TRUE(ifdefAt >= 0);
    ASSERT_TRUE(callAt > ifdefAt);
    ASSERT_TRUE(elseAt > callAt);
}

TEST(PlaybackDisplayWakeWiring, CalledExactlyThreeTimesNeverPerFrame)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // Three call sites: play-start (checked), play-stop (!checked), closeEvent -- each passes a
    // literal bool argument, unlike the definition's "( bool required )" parameter, so counting
    // the two literal-argument spellings excludes the (unqualified, since this is a free
    // function, not a member) definition line.
    const int callCount =
        countOccurrences(source, QStringLiteral("setPlaybackDisplayRequiredExecutionState( true )")) +
        countOccurrences(source, QStringLiteral("setPlaybackDisplayRequiredExecutionState( false )"));
    ASSERT_EQ(3, callCount);
}

TEST(PlaybackDisplayWakeWiring, SetOnPlayStartBeforeSmokeTelemetryBegins)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("//Zebras en-/disabled"));
    ASSERT_FALSE(toggleBody.isEmpty());

    const int checkedBlockAt = toggleBody.indexOf(QStringLiteral("if( checked )\n    {"));
    ASSERT_TRUE(checkedBlockAt >= 0);
    const QString checkedBlock = toggleBody.mid(checkedBlockAt);

    const int acquireAt = checkedBlock.indexOf(QStringLiteral("setPlaybackDisplayRequiredExecutionState( true );"));
    const int beginAt = checkedBlock.indexOf(QStringLiteral("beginPlaybackSmokeTelemetry();"));
    ASSERT_TRUE(acquireAt >= 0);
    ASSERT_TRUE(beginAt > acquireAt);
}

TEST(PlaybackDisplayWakeWiring, ReleasedOnStopUnconditionallyNotGatedOnSmokeActive)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("//Zebras en-/disabled"));
    ASSERT_FALSE(toggleBody.isEmpty());

    const int stopBlockAt = toggleBody.indexOf(QStringLiteral("if( !checked )\n    {"));
    ASSERT_TRUE(stopBlockAt >= 0);
    const int finishAt = toggleBody.indexOf(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"), stopBlockAt);
    const int releaseAt = toggleBody.indexOf(QStringLiteral("setPlaybackDisplayRequiredExecutionState( false );"), stopBlockAt);
    ASSERT_TRUE(finishAt > stopBlockAt);
    ASSERT_TRUE(releaseAt >= 0 && releaseAt < finishAt);
}

TEST(PlaybackDisplayWakeWiring, ReleasedOnWindowCloseBeforeAnyOtherCloseWork)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::closeEvent(QCloseEvent *event)"),
        QStringLiteral("//Disable WBPicker"));
    ASSERT_FALSE(body.isEmpty());

    const int releaseAt = body.indexOf(QStringLiteral("setPlaybackDisplayRequiredExecutionState( false );"));
    const int setCheckedAt = body.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    ASSERT_TRUE(releaseAt >= 0);
    ASSERT_TRUE(setCheckedAt > releaseAt);
}

TEST(PlaybackDisplayWakeWiring, BeginTelemetryLogsDisplayRequiredOncePerSession)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"),
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    ASSERT_FALSE(body.isEmpty());

    // Exactly one occurrence inside a single begin-session function body: logged once per
    // playback session, not once per frame.
    const int logLineCount = countOccurrences(body, QStringLiteral("playback_smoke.display_required session=%1"));
    ASSERT_EQ(1, logLineCount);
}

// --------------------------------------------------------------------------------------------
// CUDA-PERF-DISPLAY-WAKE-2: acquisition-outcome reporting and the screen-saver/monitor-off
// WM_SYSCOMMAND refusal (SetThreadExecutionState alone does not stop the screen saver).
// --------------------------------------------------------------------------------------------

TEST(PlaybackDisplayWakeWiring, HelperReturnsTheAcquisitionOutcomeNotJustTheRequest)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("static bool setPlaybackDisplayRequiredExecutionState( bool required )"),
        QStringLiteral("/* spaceTag argument options"));
    ASSERT_FALSE(body.isEmpty());

    // The Win32 call's return value must be captured and used as the function's own return,
    // not discarded -- a failed acquire must be visible to the caller, not assumed.
    const int callAt = body.indexOf(QStringLiteral(
        "SetThreadExecutionState( required ? ( ES_CONTINUOUS | ES_DISPLAY_REQUIRED ) : ES_CONTINUOUS );"));
    ASSERT_TRUE(callAt >= 0);
    const int returnAt = body.indexOf(QStringLiteral("return previousState != 0;"), callAt);
    ASSERT_TRUE(returnAt > callAt);
}

TEST(PlaybackDisplayWakeWiring, PlayStartCapturesTheOutcomeBeforeTelemetryLogsIt)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("//Zebras en-/disabled"));
    ASSERT_FALSE(toggleBody.isEmpty());

    const int checkedBlockAt = toggleBody.indexOf(QStringLiteral("if( checked )\n    {"));
    ASSERT_TRUE(checkedBlockAt >= 0);
    const QString checkedBlock = toggleBody.mid(checkedBlockAt);

    const int acquireAt = checkedBlock.indexOf(QStringLiteral(
        "m_playbackDisplayRequiredAcquired = setPlaybackDisplayRequiredExecutionState( true );"));
    const int beginAt = checkedBlock.indexOf(QStringLiteral("beginPlaybackSmokeTelemetry();"));
    ASSERT_TRUE(acquireAt >= 0);
    ASSERT_TRUE(beginAt > acquireAt);

    // The telemetry line reports the captured outcome, not a hardcoded true.
    const QString smokeBody = functionBody(source,
        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"),
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    ASSERT_FALSE(smokeBody.isEmpty());
    const int argAt = smokeBody.indexOf(QStringLiteral("playback_smoke.display_required session=%1"));
    ASSERT_TRUE(argAt >= 0);
    const int outcomeArgAt = smokeBody.indexOf(
        QStringLiteral(".arg( bool01( m_playbackDisplayRequiredAcquired ) );"), argAt);
    ASSERT_TRUE(outcomeArgAt > argAt);
}

TEST(PlaybackDisplayWakeWiring, ActiveFlagArmedOnPlayStartAndDisarmedOnStopAndClose)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("//Zebras en-/disabled"));
    ASSERT_FALSE(toggleBody.isEmpty());

    const int stopBlockAt = toggleBody.indexOf(QStringLiteral("if( !checked )\n    {"));
    ASSERT_TRUE(stopBlockAt >= 0);
    const int disarmAt = toggleBody.indexOf(QStringLiteral("m_playbackDisplayRequiredActive = false;"), stopBlockAt);
    const int finishAt = toggleBody.indexOf(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"), stopBlockAt);
    ASSERT_TRUE(disarmAt >= stopBlockAt && disarmAt < finishAt);

    const int checkedBlockAt = toggleBody.indexOf(QStringLiteral("if( checked )\n    {"));
    ASSERT_TRUE(checkedBlockAt >= 0);
    const QString checkedBlock = toggleBody.mid(checkedBlockAt);
    const int armAt = checkedBlock.indexOf(QStringLiteral("m_playbackDisplayRequiredActive = true;"));
    const int beginAt = checkedBlock.indexOf(QStringLiteral("beginPlaybackSmokeTelemetry();"));
    ASSERT_TRUE(armAt >= 0 && armAt < beginAt);

    const QString closeBody = functionBody(source,
        QStringLiteral("void MainWindow::closeEvent(QCloseEvent *event)"),
        QStringLiteral("//Disable WBPicker"));
    ASSERT_FALSE(closeBody.isEmpty());
    const int closeDisarmAt = closeBody.indexOf(QStringLiteral("m_playbackDisplayRequiredActive = false;"));
    const int setCheckedAt = closeBody.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    ASSERT_TRUE(closeDisarmAt >= 0 && closeDisarmAt < setCheckedAt);
}

TEST(PlaybackDisplayWakeWiring, ScreensaverBlockedCountResetOncePerSessionAtPlayStart)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggleBody = functionBody(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("//Zebras en-/disabled"));
    ASSERT_FALSE(toggleBody.isEmpty());

    const int resetCount = countOccurrences(toggleBody, QStringLiteral("m_playbackScreensaverBlockedCount = 0;"));
    ASSERT_EQ(1, resetCount);

    const int checkedBlockAt = toggleBody.indexOf(QStringLiteral("if( checked )\n    {"));
    ASSERT_TRUE(checkedBlockAt >= 0);
    const QString checkedBlock = toggleBody.mid(checkedBlockAt);
    const int resetAt = checkedBlock.indexOf(QStringLiteral("m_playbackScreensaverBlockedCount = 0;"));
    const int beginAt = checkedBlock.indexOf(QStringLiteral("beginPlaybackSmokeTelemetry();"));
    ASSERT_TRUE(resetAt >= 0 && resetAt < beginAt);
}

TEST(PlaybackDisplayWakeWiring, NativeEventIsWindowsOnlyGatedOnActiveAndRefusesBothSysCommands)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("#ifdef Q_OS_WIN\n// CUDA-PERF-DISPLAY-WAKE-2: SetThreadExecutionState(ES_DISPLAY_REQUIRED)"),
        QStringLiteral("//Disable WBPicker if picture is left"));
    ASSERT_FALSE(body.isEmpty());

    ASSERT_TRUE(body.indexOf(QStringLiteral("bool MainWindow::nativeEvent(")) >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_playbackDisplayRequiredActive")) >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("eventType == \"windows_generic_MSG\"")) >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("msg->message == WM_SYSCOMMAND")) >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("SC_SCREENSAVE")) >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("SC_MONITORPOWER")) >= 0);
    const int incrementAt = body.indexOf(QStringLiteral("++m_playbackScreensaverBlockedCount;"));
    const int returnTrueAt = body.indexOf(QStringLiteral("return true;"), incrementAt);
    ASSERT_TRUE(incrementAt >= 0);
    ASSERT_TRUE(returnTrueAt > incrementAt);
    // Falls through to the base implementation for everything else -- never swallows unrelated
    // native events.
    ASSERT_TRUE(body.indexOf(QStringLiteral("return QMainWindow::nativeEvent( eventType, message, result );")) > returnTrueAt);

    // The whole function is compiled out on non-Windows platforms.
    const int endifAt = source.indexOf(QStringLiteral("#endif"),
        source.indexOf(QStringLiteral("bool MainWindow::nativeEvent(")));
    ASSERT_TRUE(endifAt > 0);
}

TEST(PlaybackDisplayWakeWiring, SummaryLineExportsScreensaverBlockedCount)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
    ASSERT_FALSE(body.isEmpty());

    ASSERT_TRUE(body.indexOf(QStringLiteral("screensaver_blocked_count=%")) >= 0);
    const int argAt = body.indexOf(QStringLiteral(
        ".arg( static_cast<qulonglong>( m_playbackScreensaverBlockedCount ) );"));
    ASSERT_TRUE(argAt >= 0);
}
