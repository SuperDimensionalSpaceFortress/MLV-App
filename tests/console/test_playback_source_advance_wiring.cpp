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
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_playbackWrapRecorder.engineWrapsWidth( sourceAdvanceWrapsBefore,"), call) > call);
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
    ASSERT_TRUE(body.indexOf(QStringLiteral("+ static_cast<double>( dropFrameTick.wrapWidth ) * dropFrameTick.wrapLaps;")) > loopWrap);
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
