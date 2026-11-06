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
    const int loopWrap = body.indexOf(QStringLiteral("m_playbackWrapRecorder.noteEngineWrap( ui->spinBoxCutOut->value() - ui->spinBoxCutIn->value() + 1 );"));
    ASSERT_TRUE(loopWrap >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("ui->horizontalSliderPosition->setValue( cutInFrame );"), loopWrap) > loopWrap);
    // r21 (key3 M-HFR-DROP-WRAP-WIDTH): a drop-frame wrap records the width its tick subtracted (cutOut - cutIn); the non-drop
    // Loop wrap above, which steps cutOut -> cutIn, is the only site that records cutOut - cutIn + 1
    const int dropWrap = body.indexOf(QStringLiteral("if( dropFrameTick.wrapped ) m_playbackWrapRecorder.noteEngineWrap( dropFrameTick.wrapWidth );"));
    ASSERT_TRUE(dropWrap >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("ui->horizontalSliderPosition->setValue( m_newPosDropMode );"), dropWrap) > dropWrap);
    const QString loopWidth = QStringLiteral("noteEngineWrap( ui->spinBoxCutOut->value() - ui->spinBoxCutIn->value() + 1 )");
    ASSERT_EQ(body.indexOf(loopWidth), body.lastIndexOf(loopWidth));
    ASSERT_TRUE(body.indexOf(QStringLiteral("+ static_cast<double>( dropFrameTick.wrapWidth );")) > loopWrap);
    // playbackHandling only moves the target; the request and its stamp come after it, in drawFrame
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("drawFrame(")));
}
