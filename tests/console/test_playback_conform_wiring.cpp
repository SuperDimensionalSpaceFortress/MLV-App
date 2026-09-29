// PLAYBACK-HFR-CONFORM-DEFAULT-1 round 1: wiring/census test. MainWindow.cpp needs a full GUI
// build (not linked into console_tests), so this reads the source as text and pins, by marker:
//   (1) EVERY playback call site takes the conform-aware getPlaybackFramerate() -- one test per
//       site, so reverting any one of them to getFramerate() turns exactly that test red;
//   (2) the EXPORT / METADATA / TIMECODE call sites are untouched (negative test), and the
//       export FPS override (m_fpsOverride / m_frameRate) never reaches getPlaybackFramerate();
//   (3) audio is muted through ONE helper: m_tryToSyncAudio is raised in exactly one place, that
//       place and the consumer are both gated on conform, and actionAudioOutput is never unchecked;
//   (4) the Auto quality target is capped, the pacing round-up is gone, the status text and
//       settings keys are the new ones.
// (mirrors test_playback_display_wake_wiring.cpp's approach for the same reason.)
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

QString mainWindowSource()
{
    return readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
}

QString timerFrameEventBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"),
        QStringLiteral("void MainWindow::timerEvent(QTimerEvent *t)"));
}

QString playbackHandlingBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"),
        QStringLiteral("void MainWindow::showPerformanceProfilingDialog( void )"));
}

} // namespace

// ---- (1) one test per playback call site ---------------------------------------------------

TEST(PlaybackConformWiring, TimerTargetFrameMsUsesPlaybackRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "const double targetFrameMs = 1000.0 / qMax( getPlaybackFramerate(), 1.0 );")));
}

TEST(PlaybackConformWiring, PausedDropFrameTimeDiffSeedUsesPlaybackRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "timeDiff = 1000 / getPlaybackFramerate();")));
}

TEST(PlaybackConformWiring, TimerFrameEventNeverReadsTheExportRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(body.contains(QStringLiteral("getFramerate(")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_fpsOverride")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_frameRate")));
}

TEST(PlaybackConformWiring, DropFrameSteppingUsesPlaybackRate)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "m_newPosDropMode, getPlaybackFramerate() * (double)timeDiff / 1000.0,")));
    ASSERT_FALSE(body.contains(QStringLiteral("getFramerate(")));
}

TEST(PlaybackConformWiring, PlaybackTimerIsStartedAtThePlaybackRateEverywhere)
{
    const QString source = mainWindowSource();
    // clip load, export-dialog close, conform-settings change
    ASSERT_EQ(3, countOccurrences(source,
        QStringLiteral("mlvappStartPlaybackTimer( this, getPlaybackFramerate() )")));
    ASSERT_EQ(0, countOccurrences(source,
        QStringLiteral("mlvappStartPlaybackTimer( this, getFramerate() )")));
}

TEST(PlaybackConformWiring, ClipLoadRestartsTheTimerAndRefreshesTheStatusText)
{
    const QString source = mainWindowSource();
    const int at = source.indexOf(QStringLiteral("//Restart timer\n    m_timerId = mlvappStartPlaybackTimer( this, getPlaybackFramerate() );"));
    ASSERT_TRUE(at >= 0);
    ASSERT_TRUE(source.indexOf(QStringLiteral("refreshPlaybackFpsStatus();"), at) > at);
    ASSERT_TRUE(source.indexOf(QStringLiteral("refreshPlaybackFpsStatus();"), at) - at < 200);
}

TEST(PlaybackConformWiring, ContactSheetTimeAxisUsesPlaybackRate)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const double contactSheetFps = getPlaybackFramerate();")));
    ASSERT_FALSE(source.contains(QStringLiteral(
        "const double contactSheetFps = getFramerate();")));
}

// ---- (2) export / metadata / timecode are untouched -----------------------------------------

TEST(PlaybackConformWiring, GetFramerateStillCarriesTheExportOverride)
{
    const QString source = mainWindowSource();
    const QString body = functionBody(source,
        QStringLiteral("double MainWindow::getFramerate( void )"),
        QStringLiteral("double MainWindow::getPlaybackFramerate( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("if( m_fpsOverride ) return m_frameRate;")));
    ASSERT_TRUE(body.contains(QStringLiteral("return getMlvFramerate( m_pMlvObject );")));
}

TEST(PlaybackConformWiring, ExportOverrideNeverReachesThePlaybackRate)
{
    const QString source = mainWindowSource();
    const QString body = functionBody(source,
        QStringLiteral("double MainWindow::getPlaybackFramerate( void )"),
        QStringLiteral("bool MainWindow::playbackConformActive( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("playback_conform::playbackFps(")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_fpsOverride")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_frameRate")));
    ASSERT_FALSE(body.contains(QStringLiteral("getFramerate(")));
}

TEST(PlaybackConformWiring, ExportCallSitesStillReadTheExportRate)
{
    const QString source = mainWindowSource();
    // ffmpeg fps / minterpolate rate and the per-preset fps+bitrate ladders (12526-12626 area)
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral(".arg( locale.toString( getFramerate() * 2.0 ) )")));
    ASSERT_TRUE(source.contains(QStringLiteral("QString fps = locale.toString( getFramerate() );")));
    ASSERT_TRUE(countOccurrences(source, QStringLiteral("getFramerate() == 25.0")) >= 4);
    ASSERT_TRUE(countOccurrences(source, QStringLiteral("getFramerate() == 60000.0/1001.0")) >= 4);
    ASSERT_TRUE(source.contains(QStringLiteral("|| getFramerate() == 24000.0/1001.0 ) optionFps")));
    // three-pass / AVEncoder init
    const int encoderAt = source.indexOf(QStringLiteral("initAVEncoder( width,"));
    ASSERT_TRUE(encoderAt >= 0);
    ASSERT_TRUE(source.mid(encoderAt, 400).contains(QStringLiteral("getFramerate() );")));
    // scripting TIFF input fps and shutter angle / clip info are metadata at the clip's own rate
    ASSERT_EQ(3, countOccurrences(source, QStringLiteral("setNextScriptInputTiff( getMlvFramerate( m_pMlvObject ), folderName )")));
    ASSERT_TRUE(source.contains(QStringLiteral("float shutterAngle = getMlvFramerate( m_pMlvObject ) * 360.0f / shutterSpeed;")));
    ASSERT_TRUE(source.contains(QStringLiteral("QString( \"%1 fps\" ).arg( getMlvFramerate( m_pMlvObject ) )")));
}

TEST(PlaybackConformWiring, TimecodeDisplayStaysOnTheSourceExportTimeline)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(countOccurrences(source, QStringLiteral(
        "getFramerate() ).scaled( 200 * devicePixelRatio()")) >= 6);
    ASSERT_TRUE(source.contains(QStringLiteral("const double currentFps = getFramerate();")));
    ASSERT_FALSE(source.contains(QStringLiteral("getTimeCodeLabel( ui->horizontalSliderPosition->value(), getPlaybackFramerate() )")));
}

TEST(PlaybackConformWiring, BatchExportAndAudioPositionsAreUntouched)
{
    const QString batch = readRepoFile(QStringLiteral("src/batch/BatchRunner.cpp"));
    ASSERT_FALSE(batch.contains(QStringLiteral("getPlaybackFramerate")));
    ASSERT_FALSE(batch.contains(QStringLiteral("playback_conform")));
    const QString audio = readRepoFile(QStringLiteral("platform/qt/AudioPlayback.cpp"));
    ASSERT_FALSE(audio.contains(QStringLiteral("getPlaybackFramerate")));
    ASSERT_FALSE(audio.contains(QStringLiteral("playback_conform")));
    // still converts a frame number to a sample position at the clip's own fps
    ASSERT_TRUE(audio.contains(QStringLiteral("m_mlvFrameRate = getMlvFramerate( pMlvObject );")));
}

TEST(PlaybackConformWiring, ExportDialogStillPersistsTheOverrideForExportOnly)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("set.setValue( \"fpsOverride\", m_fpsOverride );")));
    ASSERT_TRUE(source.contains(QStringLiteral("set.setValue( \"frameRate\", m_frameRate );")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_fpsOverride = pExportSettings->isFpsOverride();")));
}

// ---- (3) audio mute ---------------------------------------------------------------------

TEST(PlaybackConformWiring, AudioSyncFlagIsRaisedInExactlyOneGatedPlace)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("m_tryToSyncAudio = true;")));
    const QString helper = functionBody(source,
        QStringLiteral("void MainWindow::requestPlaybackAudioSync( void )"),
        QStringLiteral("void MainWindow::refreshPlaybackFpsStatus( void )"));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_TRUE(helper.contains(QStringLiteral(
        "if( !playback_conform::audioSyncAllowed( playbackConformActive() ) ) return;")));
    ASSERT_TRUE(helper.contains(QStringLiteral("m_tryToSyncAudio = true;")));
}

TEST(PlaybackConformWiring, PlayStartLoopWrapSeekAndCutInPathsAllRouteThroughTheHelper)
{
    const QString source = mainWindowSource();
    // play start, loop wrap (x2: last-frame loop and drop-frame wrap), seek (slider), cut-in jump
    ASSERT_EQ(5, countOccurrences(source, QStringLiteral("requestPlaybackAudioSync();")));
}

TEST(PlaybackConformWiring, AudioSyncConsumerIsGatedOnConformToo)
{
    const QString source = mainWindowSource();
    const int at = source.indexOf(QStringLiteral("if( m_tryToSyncAudio && m_pAudioPlayback"));
    ASSERT_TRUE(at >= 0);
    const QString head = source.mid(at, 420);
    ASSERT_TRUE(head.contains(QStringLiteral("playback_conform::audioSyncAllowed( playbackConformActive() )")));
    // the counters the acceptance reads are only advanced inside that gated consumer
    const int counterAt = source.indexOf(QStringLiteral("++m_playbackAudioSyncRequestCount;"));
    ASSERT_TRUE(counterAt > at);
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("++m_playbackAudioSyncRequestCount;")));
}

TEST(PlaybackConformWiring, SavedAudioOutputChoiceIsNeverUncheckedByConform)
{
    const QString source = mainWindowSource();
    // only the readSettings restore ever touches setChecked on the audio action
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("ui->actionAudioOutput->setChecked(")));
    ASSERT_TRUE(source.contains(QStringLiteral("set.setValue( \"audioOutput\", ui->actionAudioOutput->isChecked() );")));
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_FALSE(apply.isEmpty());
    ASSERT_FALSE(apply.contains(QStringLiteral("actionAudioOutput->setChecked")));
}

TEST(PlaybackConformWiring, MutedStatusIsShownWhenPlaybackStartsUnderConform)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if( ui->actionAudioOutput->isChecked() && playbackConformActive() )\n"
        "        {\n"
        "            statusBar()->showMessage( playback_conform::audioMutedStatusText(), 4000 );")));
}

// ---- (4) Auto target, pacing, status, settings --------------------------------------------

TEST(PlaybackConformWiring, AutoQualityUsesTheCappedTarget)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(3, countOccurrences(source, QStringLiteral("effectivePlaybackAutoTargetFps()")));
    ASSERT_TRUE(source.contains(QStringLiteral("playback_auto_force_fast_decision(\n                            effectivePlaybackAutoTargetFps(),")));
    ASSERT_TRUE(source.contains(QStringLiteral("decideNextSlot(\n                            effectivePlaybackAutoTargetFps(),")));
    ASSERT_TRUE(source.contains(QStringLiteral("? effectivePlaybackAutoTargetFps()")));
    const QString helper = functionBody(source,
        QStringLiteral("int MainWindow::effectivePlaybackAutoTargetFps( void )"),
        QStringLiteral("//The ONLY place m_tryToSyncAudio is raised"));
    ASSERT_TRUE(helper.contains(QStringLiteral("playback_conform::effectiveAutoTargetFps(")));
    ASSERT_TRUE(helper.contains(QStringLiteral("playback_auto_target_fps_env_override() > 0")));
}

TEST(PlaybackConformWiring, EarlyAdvanceRoundUpIsReplacedByTheDebtModel)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("playback_conform::applyEarlyCredit(")));
    ASSERT_TRUE(body.contains(QStringLiteral("static int earlyCreditDebtMs = 0;")));
    // the old unconditional "timeDiff = targetFrameMsCeil" round-up on an early advance is gone
    ASSERT_FALSE(body.contains(QStringLiteral("timeDiff = targetFrameMsCeil;")));
    // the debt is dropped whenever playback is not running
    ASSERT_TRUE(body.contains(QStringLiteral("earlyCreditDebtMs = 0;")));
}

TEST(PlaybackConformWiring, StatusTextIsTheWidenedPurePolicyFunction)
{
    const QString source = mainWindowSource();
    ASSERT_FALSE(source.contains(QStringLiteral("static QString playbackFpsStatusText(")));
    ASSERT_TRUE(source.contains(QStringLiteral("playback_conform::playbackFpsStatusText(")));
    // refreshed on every clip load (see ClipLoadRestartsTheTimerAndRefreshesTheStatusText) and
    // whenever the conform settings change
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_TRUE(apply.contains(QStringLiteral("refreshPlaybackFpsStatus();")));
}

TEST(PlaybackConformWiring, ConformSettingsLiveInThePlaybackMenuUnderNewKeys)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("setupPlaybackConformMenu();")));
    const QString menu = functionBody(source,
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"),
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
    ASSERT_FALSE(menu.isEmpty());
    ASSERT_TRUE(menu.contains(QStringLiteral("ui->menuPlayback->addMenu(")));
    ASSERT_TRUE(menu.contains(QStringLiteral("playback_conform::loadSettings( set )")));
    ASSERT_FALSE(menu.contains(QStringLiteral("AutoTargetFps")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/PlaybackConformPolicy.h"));
    ASSERT_TRUE(policy.contains(QStringLiteral("\"Playback/ConformEnabled\"")));
    ASSERT_TRUE(policy.contains(QStringLiteral("\"Playback/ConformTargetFps\"")));
    ASSERT_TRUE(policy.contains(QStringLiteral("\"Playback/ConformThresholdFps\"")));
}

TEST(PlaybackConformWiring, ConformTelemetryLineCarriesTheAcceptanceInputs)
{
    const QString source = mainWindowSource();
    const int at = source.indexOf(QStringLiteral("playback_smoke.conform session=%1"));
    ASSERT_TRUE(at >= 0);
    const QString line = source.mid(at, 1600);
    for (const char * field : { "clip_fps=", "playback_fps=", "conform_active=", "export_fps_override=",
                                "audio_sync_requests=", "source_fps=", "max_jump=", "wraps=" }) {
        ASSERT_TRUE(line.contains(QString::fromLatin1(field)));
    }
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackSmokeSourceAdvance.notePresented(")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackSmokeSourceAdvance.reset();")));
}
