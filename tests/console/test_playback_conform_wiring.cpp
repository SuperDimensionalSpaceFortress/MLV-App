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

// HFR-NATIVE-60FPS-FULLRES-1 r24 (key3 B1 M-HFR-CONFORM-SKIP): the drop-frame tick applies the grant through
// conformDropFrameDelta, gated on conform, and the slip/source-advance/wrap recorders read the position it produced. The
// arithmetic is pinned by test_playback_frame_range.cpp's PlaybackConformDropFrame tests.
TEST(PlaybackConformWiring, DropFrameTickQuantisesTheConformedCatchupToWholeFrames)
{
    const QString body = playbackHandlingBody(mainWindowSource());
    ASSERT_FALSE(body.isEmpty());
    const QString call = QStringLiteral(
        "playback_frame_range::advanceDropFrameTick(\n"
        "                        m_newPosDropMode,\n"
        "                        playback_frame_range::conformDropFrameDelta(\n"
        "                            m_newPosDropMode,\n"
        "                            m_playbackPaceGuard.grant( getPlaybackFramerate() * (double)timeDiff / 1000.0, paceNowMs, playbackPaceGuardFps() ),\n"
        "                            playbackConformActive(), m_conformDropDebtFrames,\n"
        "                            playback_frame_range::conformTargetShown(\n"
        "                                m_lastPresentedPlaybackFrame, m_lastDisplayedPlaybackFrame, m_newPosDropMode,\n"
        "                                conformAcceptedTargetPresentPending(), conformTargetPresentOutstanding() ) ),");
    ASSERT_EQ(1, countOccurrences(body, call));
    // r28 (CoS B1 6714996522 on c4efe7e3): the gate keys off the frame DISPLAYED, never off one drawFrameReady accepted but has
    // not shown. finishPresentedFrame records it before its tail advance; a display change clears it with the accepted frame.
    {
        const QString src = mainWindowSource();
        ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).contains(QStringLiteral("long long m_lastDisplayedPlaybackFrame = -1;")));
        ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).contains(QStringLiteral("long long m_playbackPresentDispatchFrame = -1;")));
        const int finish = src.indexOf(QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"));
        ASSERT_TRUE(finish > 0);
        const int displayedAt = src.indexOf(QStringLiteral("    m_lastDisplayedPlaybackFrame = static_cast<long long>( displayFrame );"), finish);
        ASSERT_TRUE(displayedAt > finish);
        ASSERT_TRUE(src.indexOf(QStringLiteral("            timerFrameEvent();"), finish) > displayedAt);
        const int invalidate = src.indexOf(QStringLiteral("void MainWindow::invalidatePlaybackPrepForDisplayChange( const char *reason )"));
        ASSERT_TRUE(invalidate > 0);
        const QString invalidateBody = src.mid(invalidate, src.indexOf(QStringLiteral("\n}\n"), invalidate) - invalidate);
        ASSERT_TRUE(invalidateBody.contains(QStringLiteral("    m_lastPresentedPlaybackFrame = -1;\n    m_lastDisplayedPlaybackFrame = -1;")));
        ASSERT_EQ(3, countOccurrences(src, QStringLiteral("m_lastDisplayedPlaybackFrame")));   // the tick, the record, the clear
        // drawFrameReady's own window: from the accept until it returns (the inline present, or the enqueue that hands the
        // frame to the prep queue), the accepted frame is a present still to come -- the early advance runs inside it
        const int acceptedAt = src.indexOf(QStringLiteral("    m_lastPresentedPlaybackFrame = static_cast<long long>( display_frame );"));
        ASSERT_TRUE(acceptedAt > 0);
        const int windowAt = src.indexOf(QStringLiteral("    PlaybackPresentDispatchWindow presentDispatchWindow( m_playbackPresentDispatchFrame, static_cast<long long>( display_frame ) );"), acceptedAt);
        ASSERT_TRUE(windowAt > acceptedAt);
        ASSERT_TRUE(src.indexOf(QStringLiteral("const bool predictiveGpuTexNrEarlyAdvance ="), acceptedAt) > windowAt);
        // the pending present: the dispatch window, or the non-inline prep queue still holding the accepted target
        const int pending = src.indexOf(QStringLiteral("bool MainWindow::conformAcceptedTargetPresentPending()"));
        ASSERT_TRUE(pending > 0);
        const QString pendingBody = src.mid(pending, src.indexOf(QStringLiteral("\n}\n"), pending) - pending);
        ASSERT_TRUE(pendingBody.contains(QStringLiteral("if( target < 0 || m_lastPresentedPlaybackFrame != target ) return false;")));
        ASSERT_TRUE(pendingBody.contains(QStringLiteral("if( m_playbackPresentDispatchFrame == target ) return true;")));
        ASSERT_TRUE(pendingBody.contains(QStringLiteral("std::lock_guard<std::mutex> lk( m_playbackPrepMutex );")));
        ASSERT_TRUE(pendingBody.contains(QStringLiteral("return playbackPrepHoldsFrameLocked( m_playbackPrepPendingValid,")));
        // the worker records the in-flight task's frame
        ASSERT_EQ(1, countOccurrences(src, QStringLiteral(
            "m_playbackPrepInFlight.beginLocked( task.requestSerial, static_cast<int64_t>( task.displayFrame ) );")));
    }
    // r27 (adversary A1 on be20bc59): the repayment is gated on the target drawFrameReady accepted, and that member is set
    // there, after the forward-only guard and before the early advance that runs this tick
    const QString drawReady = mainWindowSource();
    const int accepted = drawReady.indexOf(QStringLiteral("    m_lastPresentedPlaybackFrame = static_cast<long long>( display_frame );"));
    ASSERT_TRUE(accepted > 0);
    ASSERT_TRUE(drawReady.indexOf(QStringLiteral("const bool predictiveGpuTexNrEarlyAdvance ="), accepted) > accepted);
    // fix iteration 1 (adversary A1 on d98be193): the hold needs a present that is still coming -- a render in flight for the
    // target or a lookahead request or slot for it in the active generation; nothing outstanding never holds
    const int outstanding = drawReady.indexOf(QStringLiteral("bool MainWindow::conformTargetPresentOutstanding()"));
    ASSERT_TRUE(outstanding > 0);
    const QString outstandingBody = drawReady.mid(outstanding, drawReady.indexOf(QStringLiteral("\n}\n"), outstanding) - outstanding);
    ASSERT_TRUE(outstandingBody.contains(QStringLiteral("if( !m_pRenderThread || !playbackConformActive() ) return false;")));
    ASSERT_TRUE(outstandingBody.contains(QStringLiteral("if( renderThreadBusyForPlaybackAdvance( target ) ) return true;")));
    ASSERT_TRUE(outstandingBody.contains(QStringLiteral("m_pRenderThread->hasPlaybackLookaheadRequest(")));
    // fix iteration 2 (adversary A1 on 9e88a583): a published Ready frame for the target, lookahead or not (the CPU path with
    // lookahead 0 is non-lookahead), is a present still coming -- isIdleExceptPlaybackLookahead and isIdle omit Ready slots
    ASSERT_TRUE(outstandingBody.contains(QStringLiteral("m_pRenderThread->hasReadyFrameForPlaybackTarget(")));
    const QString renderThread = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const int ready = renderThread.indexOf(QStringLiteral("bool RenderFrameThread::hasReadyFrameForPlaybackTarget("));
    ASSERT_TRUE(ready > 0);
    const QString readyBody = renderThread.mid(ready, renderThread.indexOf(QStringLiteral("\n}\n"), ready) - ready);
    // r28 (adversary A1 on c4efe7e3): published means slot.ready alone, as the acquisitions read it (Phase3Mode::Disabled
    // publishes with the state left Idle); no slot-state filter
    ASSERT_TRUE(readyBody.contains(QStringLiteral("if( !slot.ready ) continue;")));
    ASSERT_FALSE(readyBody.contains(QStringLiteral("SlotState::")));
    ASSERT_TRUE(readyBody.contains(QStringLiteral("slot.presentationContext.presentationGeneration == activeGeneration")));
    ASSERT_FALSE(readyBody.contains(QStringLiteral("playbackLookaheadRequest")));
    // adversary A1 on 9b900412: the held frames are carried and repaid, and no debt survives a Play stop or start
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral(
        "        m_playbackPaceGuard.reset();\n"
        "        m_conformDropDebtFrames = 0.0;")));
    const QString source = mainWindowSource();
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral(
        "    m_playbackPaceGuard.reset();\n"
        "    m_conformDropDebtFrames = 0.0;   // HFR-NATIVE-60FPS-FULLRES-1 r24: no held conform frame survives a Play start or stop")));
    // the tick's comment, the tick, the two clears: nothing else writes or reads it
    ASSERT_EQ(4, countOccurrences(source, QStringLiteral("m_conformDropDebtFrames")));
    ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).contains(QStringLiteral("double m_conformDropDebtFrames = 0.0;")));
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("playback_frame_range::advanceDropFrameTick(")));
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("playback_frame_range::conformDropFrameDelta(")));
    // the recorders read the tick's own position, after the quantisation
    const int at = body.indexOf(call);
    ASSERT_TRUE(body.indexOf(QStringLiteral("const double grantedDropFrames = dropFrameTick.position - sourcePositionBeforeDropTick"), at) > at);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_sourceAdvance.noteEngineTick( sourcePositionBeforeDropTick, dropFrameTick.position, dropFrameTick.wrapped );"), at) > at);
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
    // the play stop (resize, end of clip and Play off all uncheck actionPlay) forgets through the same helper, and so do
    // the two clip-open engine sites (r7, PlaybackConformWiring.AnEngineResetOrReinitForgetsTheAppliedSync)
    ASSERT_EQ(4, countOccurrences(source, QStringLiteral("forgetAppliedPlaybackAudioSync();")));

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

// MERGE-MASTER r7 (sol r6 B2): the stop census above missed the engine RESETS. resetAudioEngine at clip open closes the
// engine the last applied sync started, and a rename/reopen can keep Play on, so the new clip's slider-reset sync at the
// same frame within 100 ms was dropped as a duplicate and the new engine never started. Every live reset / re-init of the
// engine outside the consumer is pinned here and forgets the applied sync next to it (right after the reset, right before
// the re-init); a new reset or init site changes the census and must decide the same.
namespace
{
// positions of needle that are not on a // comment line
QList<int> liveOccurrences(const QString & source, const QString & needle)
{
    QList<int> out;
    for (int at = source.indexOf(needle); at >= 0; at = source.indexOf(needle, at + needle.length())) {
        const int lineStart = source.lastIndexOf(QLatin1Char('\n'), at) + 1;
        if (!source.mid(lineStart, at - lineStart).trimmed().startsWith(QStringLiteral("//"))) out.append(at);
    }
    return out;
}
}

TEST(PlaybackConformWiring, AnEngineResetOrReinitForgetsTheAppliedSync)
{
    const QString source = mainWindowSource();
    const QList<int> resets = liveOccurrences(source, QStringLiteral("m_pAudioPlayback->resetAudioEngine("));
    const QList<int> inits = liveOccurrences(source, QStringLiteral("m_pAudioPlayback->initAudioEngine("));
    // the census: one live reset and one live (re-)init, both in openMlv; the destructor's shutdownWorkerThread is teardown
    ASSERT_EQ(1, resets.size());
    ASSERT_EQ(1, inits.size());
    ASSERT_EQ(1, liveOccurrences(source, QStringLiteral("m_pAudioPlayback->shutdownWorkerThread(")).size());
    const QString open = functionBody(source, QStringLiteral("int MainWindow::openMlv( QString fileName )"),
                                      QStringLiteral("m_pAudioPlayback->initAudioEngine( m_pMlvObject );"));
    ASSERT_FALSE(open.isEmpty());
    ASSERT_TRUE(open.contains(QStringLiteral("m_pAudioPlayback->resetAudioEngine();")));
    // the forget is the statement right after the reset, and right before the re-init (whose next line the route-prewarm
    // wiring pins as armPlaybackRoutePrewarm()); only comment or blank lines may sit between
    const QString forgetCall = QStringLiteral("forgetAppliedPlaybackAudioSync();");
    auto onlyCommentsBetween = [&source](int from, int to) {
        if (to < from - 1) return false;
        if (to < from) return true;   // adjacent lines
        for (const QString & line : source.mid(from, to - from).split(QLatin1Char('\n'))) {
            const QString trimmed = line.trimmed();
            if (!trimmed.isEmpty() && !trimmed.startsWith(QStringLiteral("//"))) return false;
        }
        return true;
    };
    for (const int at : resets) {
        const int lineEnd = source.indexOf(QLatin1Char('\n'), at);
        const int forget = source.indexOf(forgetCall, lineEnd);
        ASSERT_TRUE(lineEnd > at && forget > lineEnd);
        ASSERT_TRUE(onlyCommentsBetween(lineEnd + 1, source.lastIndexOf(QLatin1Char('\n'), forget)));
    }
    for (const int at : inits) {
        const int lineStart = source.lastIndexOf(QLatin1Char('\n'), at);
        const int forget = source.lastIndexOf(forgetCall, lineStart);
        ASSERT_TRUE(forget >= 0);
        ASSERT_TRUE(onlyCommentsBetween(source.indexOf(QLatin1Char('\n'), forget) + 1, lineStart));
    }
    // the sync flag is still raised in exactly one place
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("m_tryToSyncAudio = true")));
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

// MERGE-MASTER r11 (ADV-A2): refreshPlaybackFpsStatus() installs the mute tooltip only when playbackConformActive(),
// which needs m_fileLoaded. On a clip open the status is therefore refreshed AFTER openMlv sets m_fileLoaded (the
// refresh right after the timer restart runs while it is still false and installs an empty tooltip).
TEST(PlaybackConformWiring, AdvA2ClipOpenRefreshesTheMuteTooltipOnceTheClipCountsAsLoaded)
{
    const QString source = mainWindowSource();
    const QString open = functionBody(source,
        QStringLiteral("int MainWindow::openMlv( QString fileName )"),
        QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    ASSERT_FALSE(open.isEmpty());
    ASSERT_EQ(1, countOccurrences(open, QStringLiteral("m_fileLoaded = true;")));
    const int loaded = open.indexOf(QStringLiteral("m_fileLoaded = true;"));
    // the playbackConformActive() gate the tooltip reads is the m_fileLoaded one
    const QString active = functionBody(source,
        QStringLiteral("bool MainWindow::playbackConformActive( void )"),
        QStringLiteral("int MainWindow::effectivePlaybackAutoTargetFps( void )"));
    ASSERT_TRUE(active.contains(QStringLiteral("return m_fileLoaded && m_pMlvObject")));
    const int refreshAfter = open.indexOf(QStringLiteral("refreshPlaybackFpsStatus();"), loaded);
    ASSERT_TRUE(refreshAfter > loaded);
    // in the same block, before the clip-open function goes on to paint the audio track
    const int paint = open.indexOf(QStringLiteral("paintAudioTrack();"), loaded);
    ASSERT_TRUE(paint > refreshAfter);
}

// MERGE-MASTER r11 (r11 adversary A2, same class): the tooltip also reads Audio Output, so toggling it on a loaded clip
// refreshes the status -- and only on a loaded clip (readSettings checks the action while m_pMlvObject is still null).
TEST(PlaybackConformWiring, AdvA2TogglingAudioOutputOnALoadedClipRefreshesTheMuteTooltip)
{
    const QString source = mainWindowSource();
    const QString menu = functionBody(source,
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"),
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
    ASSERT_FALSE(menu.isEmpty());
    const int at = menu.indexOf(QStringLiteral("connect( ui->actionAudioOutput, &QAction::toggled, this, [this]( bool )"));
    ASSERT_TRUE(at >= 0);
    const QString handler = menu.mid(at, 200);
    ASSERT_TRUE(handler.contains(QStringLiteral("if( m_fileLoaded && m_pMlvObject ) refreshPlaybackFpsStatus();")));
    const QString refresh = functionBody(source,
        QStringLiteral("void MainWindow::refreshPlaybackFpsStatus( void )"),
        QStringLiteral("void MainWindow::noteConformStateBeforeChange( void )"));
    ASSERT_TRUE(refresh.contains(QStringLiteral("ui->actionAudioOutput->isChecked()")));
}

// MERGE-MASTER r11 (ADV-A1b): every automation wait (autoplay, settle, play action, GUI smoke) reaches the pace probe
// only through programmaticPlayState, which notes the observation in the Play's pace history and hands the history to
// the decision; the admitted Play starts a fresh history. The decision itself is executed in test_playback_frame_range.cpp.
TEST(PlaybackConformWiring, AdvA1bTheStopDecisionJudgesThePaceThePlayHasShown)
{
    const QString source = mainWindowSource();
    const QString state = functionBody(source,
        QStringLiteral("playback_frame_range::PlayStopState MainWindow::programmaticPlayState( qint64 elapsedMs, qint64 safetyMs )"),
        QStringLiteral("bool MainWindow::programmaticPlayConsumed() const"));
    ASSERT_FALSE(state.isEmpty());
    const int note = state.indexOf(QStringLiteral("m_playPaceHistory.note( m_sourceAdvance.consumed(), elapsedMs );"));
    const int decide = state.indexOf(QStringLiteral("automationPlayPaceMode(), m_playPaceHistory );"));
    ASSERT_TRUE(note >= 0);
    ASSERT_TRUE(decide > note);
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("playback_frame_range::evaluatePlayStop(")));
    ASSERT_EQ(5, countOccurrences(source, QStringLiteral("programmaticPlayState(")));   // the 4 waits + the definition
    const QString play = functionBody(source,
        QStringLiteral("bool MainWindow::programmaticPlay("),
        QStringLiteral("void MainWindow::programmaticStop( const char *site )"));
    const int reset = play.indexOf(QStringLiteral("m_playPaceHistory = playback_frame_range::PlayPaceHistory();"));
    ASSERT_TRUE(reset >= 0);
    ASSERT_TRUE(play.indexOf(QStringLiteral("ui->actionPlay->trigger();"), reset) > reset);
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
