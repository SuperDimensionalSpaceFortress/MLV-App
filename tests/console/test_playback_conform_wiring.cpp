// PLAYBACK-HFR-CONFORM-DEFAULT-1 (re-land of #188): wiring/census test. MainWindow.cpp needs a full GUI
// build (not linked into console_tests), so this reads the source as text and pins, by marker:
//   (1) EVERY playback call site takes the conform-aware getPlaybackFramerate() -- one test per
//       site, so reverting any one of them to getFramerate() turns exactly that test red;
//   (2) the EXPORT / METADATA / TIMECODE call sites are untouched (negative test), and the
//       export FPS override (m_fpsOverride / m_frameRate) never reaches getPlaybackFramerate();
//   (3) audio is muted through ONE helper: m_tryToSyncAudio is raised in exactly one place, that
//       place and the consumer are both gated on conform, and actionAudioOutput is never unchecked;
//   (4) the Auto quality cap applies only while conform is active, the status text and settings keys
//       are the new ones, and the settings go through the automation-isolated store.
// (mirrors test_playback_native_pace_guard.cpp's wiring tests for the same reason.)
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
        QStringLiteral("void MainWindow::showPerformanceProfilingDialog"));
}

QString checkPlayableWindowBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("playback_frame_range::PlayableWindowVerdict MainWindow::checkPlayableWindow("),
        QStringLiteral("bool MainWindow::programmaticPlay("));
}

} // namespace

// ---- (1) one test per playback call site ---------------------------------------------------

TEST(PlaybackConformWiring, TimerTickShapingUsesPlaybackRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "playback_native_pace::shapedTickTimeDiffMs( timeDiff, getPlaybackFramerate(), predictivePlaybackAdvance,")));
}

TEST(PlaybackConformWiring, PausedDropFrameTimeDiffSeedUsesPlaybackRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("timeDiff = 1000 / getPlaybackFramerate();")));
}

TEST(PlaybackConformWiring, TimerFrameEventNeverReadsTheExportRate)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(body.contains(QStringLiteral("getFramerate(")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_fpsOverride")));
    ASSERT_FALSE(body.contains(QStringLiteral("m_frameRate")));
}

// Master merge (8f22c6d1, CUDA-HEADROOM-1): every guard pace argument goes through playbackPaceGuardFps(), master's one
// seam for the unpaced automation arm; its body takes the PLAYBACK rate, so the four sites still pace at it.
TEST(PlaybackConformWiring, PaceGuardHelperPacesAtPlaybackRate)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "return playback_native_pace::guardPaceFps( getPlaybackFramerate(), m_automationPacingIsolated && mlvappPlaybackUnpacedRequested() );")));
    ASSERT_FALSE(source.contains(QStringLiteral("playback_native_pace::guardPaceFps( getFramerate(),")));
}

// HUB RULING 2026-10-30T22:17:51Z (adversary A2): a play window is SOURCE seconds at the NATIVE fps, conformed or not.
// Every present advances one source frame, so N presents are N / native seconds against the clip's own floor.
TEST(PlaybackConformWiring, PresentedFramesTargetCountsSourceFramesAtTheNativeRate)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "const double presentedTargetFps = static_cast<double>( getMlvFramerate( m_pMlvObject ) );")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "if( !playback_frame_range::presentedFramesTargetReachesFloor( options.targetPresentedFrames, presentedTargetFps ) )")));
    ASSERT_FALSE(source.contains(QStringLiteral("presentedTargetFps = getPlaybackFramerate()")));
}

TEST(PlaybackConformWiring, LoopWrapPaceGuardUsesPlaybackRate)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "if( !m_playbackPaceGuard.grantLoopWrap( paceNowMs, playbackPaceGuardFps() ) ) return;")));
}

TEST(PlaybackConformWiring, WholeFramePaceGuardUsesPlaybackRate)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "if( !m_playbackPaceGuard.grantWholeFrame( paceNowMs, playbackPaceGuardFps() ) ) return;")));
}

TEST(PlaybackConformWiring, DropFrameGrantUsesPlaybackRateForBothInputs)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "m_playbackPaceGuard.grant( getPlaybackFramerate() * (double)timeDiff / 1000.0, paceNowMs, playbackPaceGuardFps() ),")));
}

TEST(PlaybackConformWiring, SlipHistogramMovesUsePlaybackRate)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    // loop wrap, whole frame, drop tick
    ASSERT_EQ(3, countOccurrences(body, QStringLiteral("getPlaybackFramerate(), !paceGuardArmedBeforeTick );")));
}

// HFR-NATIVE-60FPS-FULLRES-1 (merge of #374 with #402): the first-present pace-clock start accrues at the same
// rate grant() spends at; the export rate would bank 59.94/24 = 2.5x the credit for a conformed 59.94 clip.
TEST(PlaybackConformWiring, FirstPresentPaceClockStartUsesPlaybackRate)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "m_playbackPaceGuard.notePresent( mlv_stage_timing_now() * 1000.0, playbackPaceGuardFps() );")));
    ASSERT_FALSE(source.contains(QStringLiteral("m_playbackPaceGuard.notePresent( mlv_stage_timing_now() * 1000.0, getFramerate() );")));
}

TEST(PlaybackConformWiring, PlaybackHandlingNeverReadsTheExportRate)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
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

TEST(PlaybackConformWiring, SmokeSlipHistogramResetUsesPlaybackRate)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "m_playbackSlipHistogram.reset( m_playbackSmokeStartPosition, getPlaybackFramerate() );")));
}

// HUB RULING 2026-10-30T22:17:51Z: the window is SOURCE seconds at the NATIVE fps (master's HFR-10S-1 unit); only the
// pace is the playback rate, so a conformed HFR Play must still advance ceil(max(floor, requested) x native) frames.
TEST(PlaybackConformWiring, PlayableWindowIsSourceSecondsPacedAtPlaybackRate)
{
    const QString body = checkPlayableWindowBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("const double fps = playGateNativeFps();")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_frame_range::minPlayWindowSeconds( fps ),")));
    ASSERT_TRUE(body.contains(QStringLiteral("const double enginePaceFps = haveClip ? getPlaybackFramerate() : 0.0;")));
    ASSERT_FALSE(body.contains(QStringLiteral("const double fps = haveClip ? getPlaybackFramerate() : 0.0;")));
    ASSERT_FALSE(body.contains(QStringLiteral("= getFramerate();")));
}

TEST(PlaybackConformWiring, GuiSmokeClipLengthGuardIsSourceSecondsAtTheNativeRate)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const double guardFps = static_cast<double>( getMlvFramerate( m_pMlvObject ) );")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const double guardFloorSeconds = playback_frame_range::minPlayWindowSeconds( guardFps );")));
    ASSERT_FALSE(source.contains(QStringLiteral(
        "const double guardFps = static_cast<double>( getPlaybackFramerate() );")));
}

TEST(PlaybackConformWiring, ContactSheetTimeAxisUsesPlaybackRate)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("const double contactSheetFps = getPlaybackFramerate();")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_contactSheetCaptureFps = getPlaybackFramerate();")));
    ASSERT_FALSE(source.contains(QStringLiteral("contactSheetFps = getFramerate();")));
    ASSERT_FALSE(source.contains(QStringLiteral("m_contactSheetCaptureFps = getFramerate();")));
}

// ---- (2) export / metadata / timecode are untouched -----------------------------------------

TEST(PlaybackConformWiring, GetFramerateStillCarriesTheExportOverrideAndEnforce3Isolation)
{
    const QString source = mainWindowSource();
    const QString body = functionBody(source,
        QStringLiteral("double MainWindow::getFramerate( void )"),
        QStringLiteral("double MainWindow::getPlaybackFramerate( void )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("if( m_fpsOverride && !m_automationPacingIsolated ) return m_frameRate;")));
    ASSERT_TRUE(body.contains(QStringLiteral("return getMlvFramerate( m_pMlvObject );")));
}

// Mutation M2 (getPlaybackFramerate returns m_frameRate when m_fpsOverride) is killed here.
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

// HFR-NATIVE-D4-HEAD-VISUAL-CELLS-1 (POLICY-CALLSITE-BOOL-MUTATION-SMOKE-1): the four conform policy calls are pinned with
// their FULL argument list, so a flipped bool argument (!m_conformEnabled, !saved.enabled, !playbackConformActive()) fails here.
TEST(PlaybackConformWiring, ConformPolicyCallsPassTheirFullArgumentLists)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "return playback_conform::playbackFps( getMlvFramerate( m_pMlvObject ), m_conformEnabled, m_conformTargetFps, m_conformThresholdFps );")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "&& playback_conform::conformApplies( getMlvFramerate( m_pMlvObject ), m_conformEnabled, m_conformTargetFps, m_conformThresholdFps );")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "return playback_conform::effectiveAutoTargetFps( m_playbackAutoTargetFps, m_pMlvObject ? getPlaybackFramerate() : 0.0, playbackConformActive() );")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "m_conformEnabled = playback_conform::resolveConformEnabled( playback_conform::conformEnvOverride(), saved.enabled );")));
}

TEST(PlaybackConformWiring, ExportCallSitesStillReadTheExportRate)
{
    const QString source = mainWindowSource();
    // ffmpeg fps / minterpolate rate and the per-preset fps+bitrate ladders
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral(".arg( locale.toString( getFramerate() * 2.0 ) )")));
    ASSERT_TRUE(source.contains(QStringLiteral("QString fps = locale.toString( getFramerate() );")));
    ASSERT_TRUE(countOccurrences(source, QStringLiteral("getFramerate() == 25.0")) >= 4);
    ASSERT_TRUE(countOccurrences(source, QStringLiteral("getFramerate() == 60000.0/1001.0")) >= 4);
    ASSERT_TRUE(source.contains(QStringLiteral("|| getFramerate() == 24000.0/1001.0 ) optionFps")));
    // AVFoundation encoder init
    const int encoderAt = source.indexOf(QStringLiteral("initAVEncoder( width,"));
    ASSERT_TRUE(encoderAt >= 0);
    ASSERT_TRUE(source.mid(encoderAt, 400).contains(QStringLiteral("getFramerate() );")));
    // scripting TIFF input fps is metadata at the clip's own rate
    ASSERT_EQ(3, countOccurrences(source, QStringLiteral("setNextScriptInputTiff( getMlvFramerate( m_pMlvObject ), folderName )")));
}

TEST(PlaybackConformWiring, TimecodeDisplayStaysOnTheSourceExportTimeline)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(6, countOccurrences(source, QStringLiteral("getFramerate() ).scaled( 200 * devicePixelRatio()")));
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
    // play start, loop wrap (x2: last-frame loop and drop-frame wrap), seek (slider), go-to-first,
    // and (MERGE-MASTER r5, AUDIO-A1) the re-arm when a conform control un-conforms the clip during playback
    ASSERT_EQ(6, countOccurrences(source, QStringLiteral("requestPlaybackAudioSync();")));
    const QString handling = playbackHandlingBody(source);
    ASSERT_EQ(2, countOccurrences(handling, QStringLiteral("requestPlaybackAudioSync();")));
}

TEST(PlaybackConformWiring, AudioSyncConsumerIsGatedOnConformToo)
{
    const QString source = mainWindowSource();
    const int at = source.indexOf(QStringLiteral("if( m_tryToSyncAudio && m_pAudioPlayback"));
    ASSERT_TRUE(at >= 0);
    const QString head = source.mid(at, 420);
    ASSERT_TRUE(head.contains(QStringLiteral("playback_conform::audioSyncAllowed( playbackConformActive() )")));
    // the counter the acceptance reads is only advanced inside that gated consumer
    const int counterAt = source.indexOf(QStringLiteral("++m_playbackAudioSyncRequestCount;"));
    ASSERT_TRUE(counterAt > at);
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("++m_playbackAudioSyncRequestCount;")));
}

// MERGE-MASTER r5 (AUDIO-A1, r4 adversary successor ADV-A1, Dot 6542640078): a conform target/threshold/toggle change before
// the first present cleared the pending audio start unconditionally (m_tryToSyncAudio = false), even for a 23.976 clip it
// left non-conformed; with Loop off no drop-frame tick re-armed it, so the prepared sink stayed silent. The change now goes
// through playback_conform::pendingAudioStartOnConformChange (behaviour: PlaybackConformPendingAudio.*), fed the state each
// control recorded BEFORE it changed the settings.
TEST(PlaybackConformWiring, ConformControlTouchesThePendingAudioStartOnlyOnAConformChange)
{
    const QString source = mainWindowSource();
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_FALSE(apply.isEmpty());
    // the FULL argument list, so negating any argument (e.g. !playbackConformActive()) breaks the pin
    const int decide = apply.indexOf(QStringLiteral(
        "switch( playback_conform::pendingAudioStartOnConformChange( m_conformActiveBeforeChange, m_conformPlaybackFpsBeforeChange,\n"
        "                                                                playbackConformActive(), m_pMlvObject ? getPlaybackFramerate() : 0.0 ) )"));
    ASSERT_TRUE(decide >= 0);
    // nothing before the decision writes the pending flag (the unconditional clear is gone)
    ASSERT_FALSE(apply.left(decide).contains(QStringLiteral("m_tryToSyncAudio")));
    const QString decision = apply.mid(decide);
    const int clear = decision.indexOf(QStringLiteral("case playback_conform::PendingAudioStart::Clear:"));
    const int rearm = decision.indexOf(QStringLiteral("case playback_conform::PendingAudioStart::Rearm:"));
    const int keep = decision.indexOf(QStringLiteral("case playback_conform::PendingAudioStart::Keep:"));
    ASSERT_TRUE(clear > 0);
    ASSERT_TRUE(rearm > clear);
    ASSERT_TRUE(keep > rearm);
    ASSERT_TRUE(decision.mid(clear, rearm - clear).contains(QStringLiteral("m_tryToSyncAudio = false;")));
    // the re-arm goes through the conform-gated helper, under the same play/audio/drop-frame gate as go-to-first
    const QString rearmBody = decision.mid(rearm, keep - rearm);
    ASSERT_TRUE(rearmBody.contains(QStringLiteral("requestPlaybackAudioSync();")));
    ASSERT_TRUE(rearmBody.contains(QStringLiteral("ui->actionPlay->isChecked()")));
    ASSERT_TRUE(rearmBody.contains(QStringLiteral("ui->actionAudioOutput->isChecked()")));
    ASSERT_TRUE(rearmBody.contains(QStringLiteral("ui->actionDropFrameMode->isChecked()")));
    // Keep leaves the pending start alone
    const int keepEnd = decision.indexOf(QStringLiteral("break;"), keep);
    ASSERT_TRUE(keepEnd > keep);
    ASSERT_FALSE(decision.mid(keep, keepEnd - keep).contains(QStringLiteral("m_tryToSyncAudio")));

    // every conform control records the before-state FIRST, then changes the settings, then applies them
    const QString menu = functionBody(source,
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"),
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
    ASSERT_FALSE(menu.isEmpty());
    ASSERT_EQ(3, countOccurrences(menu, QStringLiteral("noteConformStateBeforeChange();")));
    ASSERT_EQ(3, countOccurrences(menu, QStringLiteral("applyPlaybackConformSettings( true );")));
    for (const char * mutation : { "m_conformEnabledSetting = on;", "m_conformTargetFps = target;", "m_conformThresholdFps = threshold;" }) {
        const int at = menu.indexOf(QString::fromLatin1(mutation));
        ASSERT_TRUE(at > 0);
        const int note = menu.lastIndexOf(QStringLiteral("noteConformStateBeforeChange();"), at);
        ASSERT_TRUE(note >= 0);
        ASSERT_TRUE(at - note < 80);
        ASSERT_TRUE(menu.indexOf(QStringLiteral("applyPlaybackConformSettings( true );"), at) > at);
    }
    const QString note = functionBody(source,
        QStringLiteral("void MainWindow::noteConformStateBeforeChange( void )"),
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"));
    ASSERT_TRUE(note.contains(QStringLiteral("m_conformActiveBeforeChange = playbackConformActive();")));
    ASSERT_TRUE(note.contains(QStringLiteral("m_conformPlaybackFpsBeforeChange = m_pMlvObject ? getPlaybackFramerate() : 0.0;")));
}

// MERGE-MASTER r6 (AUDIO-A1 residual, r5 adversary successor ADV-A1, Dot 6544364013): a conform change that leaves conform
// active stops the audio while Play stays on; un-conforming again within 100 ms at the same frame re-armed a start the
// consumer then dropped as a duplicate of the sync applied before the stop. The stop forgets the applied sync, and the
// consumer decides through the pure helper (behaviour: PlaybackConformPendingAudio.AConformOnOff*,
// PlaybackAudioStartPolicy.ASecondSyncAtTheSameFrameWithinTheWindowIsTheSameSync).
TEST(PlaybackConformWiring, AConformStopForgetsTheAppliedSyncSoARearmIsApplied)
{
    const QString source = mainWindowSource();
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_FALSE(apply.isEmpty());
    const int stop = apply.indexOf(QStringLiteral(
        "    if( m_pAudioPlayback && playbackConformActive() )\n"
        "    {\n"
        "        m_pAudioPlayback->stop();\n"
        "        forgetAppliedPlaybackAudioSync();\n"
        "    }\n"));
    ASSERT_TRUE(stop >= 0);
    ASSERT_EQ(1, countOccurrences(apply, QStringLiteral("m_pAudioPlayback->stop();")));
    const int decide = apply.indexOf(QStringLiteral("switch( playback_conform::pendingAudioStartOnConformChange("));
    ASSERT_TRUE(decide > stop);

    // the helper forgets both halves of the memo, and only it and the consumer write them
    const QString forget = functionBody(source,
        QStringLiteral("void MainWindow::forgetAppliedPlaybackAudioSync( void )"),
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"));
    ASSERT_FALSE(forget.isEmpty());
    ASSERT_TRUE(forget.contains(QStringLiteral("m_lastPlaybackAudioSyncFrame = -1;")));
    ASSERT_TRUE(forget.contains(QStringLiteral("m_lastPlaybackAudioSyncTime = 0.0;")));
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral("m_lastPlaybackAudioSyncFrame = ")));
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral("m_lastPlaybackAudioSyncTime = ")));
    // the play stop (resize, end of clip and Play off all uncheck actionPlay) forgets through the same helper
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral("forgetAppliedPlaybackAudioSync();")));

    // the consumer: the duplicate test is the pure helper, fed the memo, the presented sync frame and the clock
    const int consumer = source.indexOf(QStringLiteral("if( m_tryToSyncAudio && m_pAudioPlayback"));
    ASSERT_TRUE(consumer >= 0);
    const QString body = source.mid(consumer, 1200);
    ASSERT_TRUE(body.contains(QStringLiteral(
        "const bool duplicateRecentSync = playback_audio_start::isDuplicateSync( m_lastPlaybackAudioSyncFrame,\n"
        "                                                                            m_lastPlaybackAudioSyncTime, syncFrame, syncNow );")));
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_tryToSyncAudio = false;")) < body.indexOf(QStringLiteral("duplicateRecentSync")));

    // the census of audio stops: destructor, resize, end of clip, conform, Play off, and the legacy consumer. A new one
    // must decide whether it forgets the applied sync (it does when Play can stay on after it).
    ASSERT_EQ(6, countOccurrences(source, QStringLiteral("m_pAudioPlayback->stop()")));
}

TEST(PlaybackConformWiring, SavedAudioOutputChoiceIsNeverUncheckedByConform)
{
    const QString source = mainWindowSource();
    // only the readSettings restore ever touches setChecked on the audio action
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("ui->actionAudioOutput->setChecked(")));
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
    const QString refresh = functionBody(source,
        QStringLiteral("void MainWindow::refreshPlaybackFpsStatus( void )"),
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"));
    ASSERT_TRUE(refresh.contains(QStringLiteral("playback_conform::audioMutedStatusText()")));
}

// ---- (4) Auto target, status, settings --------------------------------------------------

TEST(PlaybackConformWiring, AutoQualityUsesTheConditionallyCappedTarget)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("playback_auto_force_fast_decision(\n                            effectivePlaybackAutoTargetFps(),")));
    ASSERT_TRUE(source.contains(QStringLiteral("decideNextSlot(\n                            effectivePlaybackAutoTargetFps(),")));
    ASSERT_TRUE(source.contains(QStringLiteral("? effectivePlaybackAutoTargetFps()")));
    const QString helper = functionBody(source,
        QStringLiteral("int MainWindow::effectivePlaybackAutoTargetFps( void )"),
        QStringLiteral("void MainWindow::requestPlaybackAudioSync( void )"));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_TRUE(helper.contains(QStringLiteral("playback_auto_target_fps_env_override() > 0")));
    // the cap is told whether conform is active (the pure policy returns the user's target otherwise)
    ASSERT_TRUE(helper.contains(QStringLiteral("playbackConformActive() );")));
}

TEST(PlaybackConformWiring, NoEarlyCreditPacingNativePaceGuardStaysTheOnlyPacing)
{
    const QString body = timerFrameEventBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(body.contains(QStringLiteral("applyEarlyCredit")));
    ASSERT_FALSE(body.contains(QStringLiteral("earlyCreditDebtMs")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/PlaybackConformPolicy.h"));
    ASSERT_FALSE(policy.contains(QStringLiteral("applyEarlyCredit")));
    ASSERT_FALSE(policy.contains(QStringLiteral("ElapsedCredit")));
}

TEST(PlaybackConformWiring, StatusTextIsTheWidenedPurePolicyFunction)
{
    const QString source = mainWindowSource();
    ASSERT_FALSE(source.contains(QStringLiteral("static QString playbackFpsStatusText(")));
    ASSERT_EQ(4, countOccurrences(source, QStringLiteral("playback_conform::playbackFpsStatusText(")));
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_TRUE(apply.contains(QStringLiteral("refreshPlaybackFpsStatus();")));
}

TEST(PlaybackConformWiring, ConformSettingsLiveInThePlaybackMenuInTheIsolatedStore)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral("setupPlaybackConformMenu();")));
    const QString menu = functionBody(source,
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"),
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
    ASSERT_FALSE(menu.isEmpty());
    ASSERT_TRUE(menu.contains(QStringLiteral("ui->menuPlayback->addMenu(")));
    ASSERT_TRUE(menu.contains(QStringLiteral("playback_conform::loadSettings( set )")));
    ASSERT_TRUE(menu.contains(QStringLiteral("automation_settings::openAppSettings()")));
    ASSERT_FALSE(menu.contains(QStringLiteral("QSettings::UserScope")));
    ASSERT_FALSE(menu.contains(QStringLiteral("AutoTargetFps")));
    const QString apply = functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
    ASSERT_TRUE(apply.contains(QStringLiteral("automation_settings::openAppSettings()")));
    ASSERT_FALSE(apply.contains(QStringLiteral("QSettings::UserScope")));
}

TEST(PlaybackConformWiring, ConformTelemetryLineCarriesTheAcceptanceInputs)
{
    const QString source = mainWindowSource();
    const int at = source.indexOf(QStringLiteral("playback_smoke.conform session=%1"));
    ASSERT_TRUE(at >= 0);
    const QString line = source.mid(at, 1600);
    for (const char * field : { "clip_fps=", "playback_fps=", "conform_active=", "export_fps_override=",
                                "audio_sync_requests=", "source_fps=", "max_jump=", "wraps=",
                                "effective_auto_target_fps=" }) {
        ASSERT_TRUE(line.contains(QString::fromLatin1(field)));
    }
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackSmokeSourceAdvance.notePresented(")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackSmokeSourceAdvance.reset();")));
}
