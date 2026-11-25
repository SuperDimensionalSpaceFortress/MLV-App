// HFR-NATIVE-60FPS-FULLRES-1 r20 (adversary A1 and A2 on dbeb9424): where MainWindow reads the source-advance tracker's
// stamp. MainWindow.cpp needs a full GUI build (not linked into console_tests), so this reads the source as text, as
// test_contact_sheet_capture_wiring.cpp does for the same reason. The helpers' behaviour is in
// test_playback_source_advance_tracker.cpp; this pins the three facts that behaviour rests on.
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

QString functionBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int end = source.indexOf(QStringLiteral("\n}\n"), at);
    ASSERT_TRUE(end > at);
    return source.mid(at, end - at);
}

} // namespace

// A lookahead is stamped after the check that proves it is the engine's target, and before the draw's own advance. The
// advance can wrap the engine and request the next frame, and either would stamp the frame with a later loop's count.
TEST(PlaybackSourceAdvanceWiring, ALookaheadIsStampedBetweenItsTargetCheckAndTheEarlyAdvance)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                                      QStringLiteral("void MainWindow::drawFrameReady()"));
    const int check = body.indexOf(QStringLiteral("if( !targetReached )"));
    const int stamp = body.indexOf(QStringLiteral("presentationAcceptedLookaheadStamp("));
    const int advance = body.indexOf(QStringLiteral("timerFrameEvent( predictiveGpuTexNrEarlyAdvance )"));
    ASSERT_TRUE(check >= 0);
    ASSERT_TRUE(stamp > check);
    ASSERT_TRUE(advance > stamp);
    ASSERT_TRUE(body.indexOf(QStringLiteral("requestContext.engineWrapsAtRequest ="), check) > check);
    ASSERT_TRUE(body.indexOf(QStringLiteral("requestContext.engineWrapsAtRequest ="), check) < stamp);
    // the frame reaches the prep task and the present path carrying that stamp
    ASSERT_TRUE(body.indexOf(QStringLiteral("task.requestContext = requestContext;")) > stamp);
    // the frame the check accepted is a lookahead equal to the active target, and nothing re-reads the target in between
    ASSERT_TRUE(body.indexOf(QStringLiteral("static_cast<int>( display_frame ) == activePlaybackTarget")) < check);
    ASSERT_EQ(-1, body.mid(check, stamp - check).indexOf(QStringLiteral("playbackHandling")));
}

// The tracker reads the stamp the frame carries. A record outside the frame is what a later request could overwrite.
TEST(PlaybackSourceAdvanceWiring, TheTrackerReadsTheFramesOwnStampNotAnEngineRecord)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                                      QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    const int call = body.indexOf(QStringLiteral("presentationLoopWraps( m_playbackWrapRecorder.engineWraps,"));
    ASSERT_TRUE(call >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("requestContext.engineWrapsAtRequest,"), call) > call);
    // and a landing is credited the cut width each wrap had when the engine made it, not the cut range as it stands now
    // (PLAYBACK-STALE-POSITION-FOLD-CONTRACT-1 r1: through paintCreditHandOff, which reads engineWrapsWidth from the first crossed wrap)
    ASSERT_TRUE(body.indexOf(QStringLiteral("paintCreditHandOff( m_playbackWrapRecorder, sourceAdvanceWrapsBefore,"), call) > call);
    ASSERT_FALSE(readRepoFile(QStringLiteral("platform/qt/MainWindow.h")).contains(QStringLiteral("m_playbackEngineRequest")));
}

// Direction decides nothing: the helper takes no frame number, so a backward seek stamped before a wrap cannot take it.
TEST(PlaybackSourceAdvanceWiring, AnEngineWrapIsNotedBeforeTheFrameItLandsOnIsRequested)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                                      QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    // r21 (adversary A1 on 43db3021): the non-drop Loop wrap records the distance from the playhead back to cut-in plus its frame
    const int loopWrap = body.indexOf(QStringLiteral("m_playbackWrapRecorder.noteEngineWrap( playback_frame_range::loopWrapWidth( ui->horizontalSliderPosition->value(), cutInFrame ) );"));
    ASSERT_TRUE(loopWrap >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("ui->horizontalSliderPosition->setValue( cutInFrame );"), loopWrap) > loopWrap);
    // r21 (key3 M-HFR-DROP-WRAP-WIDTH): a drop-frame wrap records the width its tick subtracted (cutOut - cutIn); r33 (adversary
    // A1 on e0947e37): once per lap the tick crossed
    const int dropWrap = body.indexOf(QStringLiteral("if( dropFrameTick.wrapped ) m_playbackWrapRecorder.noteEngineWrap( dropFrameTick.wrapWidth, dropFrameTick.wrapLaps );"));
    ASSERT_TRUE(dropWrap >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("ui->horizontalSliderPosition->setValue( m_newPosDropMode );"), dropWrap) > dropWrap);
    // PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 r8 iteration 1 (pass 14 A1 on a2983bf2): the granted share is the delta the tick
    // applied, every lap included (DropFrameTickResult::appliedFrames), no longer position + wrapWidth * wrapLaps
    ASSERT_TRUE(body.indexOf(QStringLiteral("const double grantedDropFrames = dropFrameTick.appliedFrames;")) > loopWrap);
    // and no wrap is credited the cut range as it stands (r21: neither engine moves by it once the cut changes under the playhead)
    const QString note = QStringLiteral("m_playbackWrapRecorder.noteEngineWrap(");
    const int firstNote = body.indexOf(note);
    const int secondNote = body.indexOf(note, firstNote + 1);
    ASSERT_TRUE(firstNote >= 0);
    ASSERT_TRUE(secondNote > firstNote);
    ASSERT_EQ(-1, body.indexOf(note, secondNote + 1));
    ASSERT_FALSE(body.contains(QStringLiteral("noteEngineWrap( ui->spinBoxCutOut->value()")));
    // playbackHandling only moves the target; the request and its stamp come after it, in drawFrame
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("drawFrame(")));
}

// PLAYBACK-STALE-POSITION-FOLD-CONTRACT-1 r1: a POSITION CORRECTION travels the engine -> paint-tracker seam the wraps take. The drop
// tick records it before its own laps (so they count as travel after it), every reader of the tick's start reads the corrected
// start, the audio resyncs on it, and the slip histogram is told of it before the tick's move.
TEST(PlaybackSourceAdvanceWiring, R1ACorrectionIsRecordedBeforeTheTicksLapsAndEveryStartReaderReadsTheCorrectedStart)
{
    const QString body = functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
                                      QStringLiteral("void MainWindow::playbackHandling(int timeDiff)"));
    const int correction = body.indexOf(QStringLiteral(
        "if( dropFrameTick.corrected ) m_playbackWrapRecorder.noteEngineCorrection( playback_frame_range::dropFrameCorrectionFrame( dropFrameTick.correctedStart ) );"));
    const int dropWrap = body.indexOf(QStringLiteral("if( dropFrameTick.wrapped ) m_playbackWrapRecorder.noteEngineWrap( dropFrameTick.wrapWidth, dropFrameTick.wrapLaps );"));
    ASSERT_TRUE(correction >= 0);
    ASSERT_TRUE(dropWrap > correction);
    ASSERT_EQ(1, body.count(QStringLiteral("m_playbackWrapRecorder.noteEngineCorrection(")));
    // iteration 1 (adversary pass 1 A1 on fc9e3e93): the counter, the slip path and the startup skips read whether the tick LAPPED
    // (dropFrameTickLapped), never the r9 fold's wrapped flag, so the real step after a zero-lap fold is counted
    const int lapped = body.indexOf(QStringLiteral("const bool dropFrameLapped = playback_frame_range::dropFrameTickLapped( dropFrameTick );"));
    ASSERT_TRUE(lapped >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_sourceAdvance.noteEngineTick( dropFrameTick.correctedStart, dropFrameTick.position, dropFrameLapped );")) > lapped);
    ASSERT_TRUE(body.indexOf(QStringLiteral("dropFrameLapped ? playback_slip::AdvancePath::LoopWrap")) > lapped);
    ASSERT_TRUE(body.indexOf(QStringLiteral("if( !dropFrameLapped\n")) > lapped);
    ASSERT_FALSE(body.contains(QStringLiteral("dropFrameTick.wrapped ? playback_slip::AdvancePath::LoopWrap")));
    ASSERT_FALSE(body.contains(QStringLiteral("if( !dropFrameTick.wrapped")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_native_pace::skippedFrames(\n                            dropFrameTick.correctedStart, dropFrameTick.position );")));
    ASSERT_FALSE(body.contains(QStringLiteral("sourcePositionBeforeDropTick")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( ( dropFrameTick.wrapped || dropFrameTick.corrected ) && ui->actionAudioOutput->isChecked() )")));
    const int slipCorrection = body.indexOf(QStringLiteral(
        "if( dropFrameTick.corrected ) m_playbackSlipHistogram.noteTimelineCorrection( playback_frame_range::dropFrameCorrectionFrame( dropFrameTick.correctedStart ) );"));
    ASSERT_TRUE(slipCorrection > dropWrap);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_playbackSlipHistogram.noteTimelineMove("), slipCorrection) > slipCorrection);
}

// r1: both stamp sites stamp the correction count beside the wrap count, and the hand-off passes the rebase to the ledger.
TEST(PlaybackSourceAdvanceWiring, R1TheCorrectionCountIsStampedAndTheHandOffPassesTheRebase)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString draw = functionBody(source, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )"));
    const int wrapStamp = draw.indexOf(QStringLiteral("requestContext.engineWrapsAtRequest = m_playbackWrapRecorder.engineWraps;"));
    ASSERT_TRUE(wrapStamp >= 0);
    ASSERT_TRUE(draw.indexOf(QStringLiteral("requestContext.engineCorrectionsAtRequest = m_playbackWrapRecorder.engineCorrections;"), wrapStamp) > wrapStamp);
    const QString ready = functionBody(source, QStringLiteral("void MainWindow::drawFrameReady()"));
    const int lookahead = ready.indexOf(QStringLiteral("presentationAcceptedLookaheadStamp( requestContext.engineWrapsAtRequest,"));
    ASSERT_TRUE(lookahead >= 0);
    const int correctionStamp = ready.indexOf(QStringLiteral("presentationAcceptedLookaheadStamp( requestContext.engineCorrectionsAtRequest,"));
    ASSERT_TRUE(correctionStamp > lookahead);
    ASSERT_TRUE(ready.indexOf(QStringLiteral("m_playbackWrapRecorder.engineCorrections );"), correctionStamp) > correctionStamp);
    ASSERT_TRUE(ready.indexOf(QStringLiteral("readyFrame.presentationContext.engineCorrectionsAtRequest = requestContext.engineCorrectionsAtRequest;"), correctionStamp) > correctionStamp);
    ASSERT_TRUE(ready.indexOf(QStringLiteral("timerFrameEvent( predictiveGpuTexNrEarlyAdvance )")) > correctionStamp);
    const QString presented = functionBody(source, QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    const int crossed = presented.indexOf(QStringLiteral("presentationLoopWraps( m_playbackWrapRecorder.engineCorrections,"));
    ASSERT_TRUE(crossed >= 0);
    ASSERT_TRUE(presented.indexOf(QStringLiteral("requestContext.engineCorrectionsAtRequest,"), crossed) > crossed);
    const int handOff = presented.indexOf(QStringLiteral("paintCreditHandOff( m_playbackWrapRecorder, sourceAdvanceWrapsBefore,"));
    ASSERT_TRUE(handOff > crossed);
    ASSERT_TRUE(presented.indexOf(QStringLiteral("sourceAdvanceHandOff.rebase, sourceAdvanceHandOff.rebaseFrame,"), handOff) > handOff);
    ASSERT_TRUE(presented.indexOf(QStringLiteral("sourceAdvanceHandOff.wrapsWidth,"), handOff) > handOff);
    // the live count is taken at the measured Play's start, like the wraps
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackSmokeTrackerEngineCorrections = m_playbackWrapRecorder.engineCorrections;")));
    ASSERT_TRUE(readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.h")).contains(QStringLiteral("int engineCorrectionsAtRequest = 0;")));
}

// PLAYBACK-STALE-POSITION-FOLD-CONTRACT-1 r2 (r1 adversary pass 2 A1): the slip sample carries the corrections its request crossed and
// the hand-off's rebase, so the slip histogram rebases its display endpoint where the paint tracker rebases; the first present's timeline
// delta and the session's timeline numerators leave out the same signed correction sum.
TEST(PlaybackSourceAdvanceWiring, R2TheSlipSampleCarriesTheLandingAndTheTimelineNumeratorsLeaveOutTheCorrections)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString presented = functionBody(source, QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    const int handOff = presented.indexOf(QStringLiteral("const playback_frame_range::PaintCreditHandOff sourceAdvanceHandOff ="));
    const int sample = presented.indexOf(QStringLiteral("playback_slip::PresentSample slipSample;"));
    const int note = presented.indexOf(QStringLiteral("m_playbackSlipHistogram.notePresent( slipSample );"));
    ASSERT_TRUE(handOff >= 0);
    ASSERT_TRUE(sample > handOff);
    ASSERT_TRUE(note > sample);
    const QString fields[] = {
        QStringLiteral("slipSample.correctionsLanded = m_playbackSmokeTrackerEngineCorrections - sourceAdvanceCorrectionsBefore;"),
        QStringLiteral("slipSample.rebase = sourceAdvanceHandOff.rebase;"),
        QStringLiteral("slipSample.rebaseFrame = sourceAdvanceHandOff.rebaseFrame;"),
        QStringLiteral("slipSample.rebaseLapped = sourceAdvanceHandOff.wrapsAfterRebase > 0;") };
    for( const QString & field : fields )
    {
        const int at = presented.indexOf(field, sample);
        ASSERT_TRUE(at > sample);
        ASSERT_TRUE(at < note);
        ASSERT_EQ(1, presented.count(field));
    }
    // iteration 1 (adversary pass 3 A3 on c4fa34c3): both read the histogram's 64-bit travel, clamped into int
    ASSERT_TRUE(presented.contains(QStringLiteral(
        "            qAbs( m_playbackSlipHistogram.timelineTravel( ui->horizontalSliderPosition->value() ) );")));
    const QString finish = functionBody(source, QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry("));
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "const int timelineDelta = m_playbackSlipHistogram.timelineTravel( currentPosition );")));
    ASSERT_FALSE(source.contains(QStringLiteral("- m_playbackSlipHistogram.correctedFrames()")));
    ASSERT_FALSE(finish.contains(QStringLiteral("const int timelineDelta = currentPosition - m_playbackSmokeStartPosition;")));
    // the slip summary and pace_summary read the same corrected timeline delta (finish() subtracts the sum itself)
    ASSERT_TRUE(finish.indexOf(QStringLiteral("const int timelineDeltaAbs = timelineDelta >= 0")) > finish.indexOf(QStringLiteral("const int timelineDelta =")));
}
