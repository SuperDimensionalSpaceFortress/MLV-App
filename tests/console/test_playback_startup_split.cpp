// PLAYBACK-STARTUP-WAIT-PREWARM-1: playback_smoke.startup_split splits the Play -> first-present wait (render
// request, first render, first GUI present, the finishPresentedFrame sub-steps and the audio start in them). The
// pure arithmetic is tested here, and the line is pinned: its field names and order, and where MainWindow feeds it.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackStartupSplit.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <string>

using playback_startup_split::StartupSplit;

namespace
{

QString mainWindowSource()
{
    const QString path = repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString bodyBetween(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackStartupSplit, TheLineCarriesEveryFieldInOrder)
{
    StartupSplit split;
    split.noteRequest(1.5);
    split.noteFirstPresent(230.0, 380.0);
    split.noteLaterPresent(13.0, 20.0);
    split.noteFirstFinishStep(playback_startup_split::StepLabels, 4.0);
    split.noteFirstAudio(0.25, 0.125, 600.0);
    split.noteFirstAudioSync(600.5);
    split.noteFirstFinish(610.0);
    ASSERT_EQ(std::string(
                  "playback_smoke.startup_split session=3 play_to_request_ms=1.500 render_first_ms=230.000 "
                  "gui_present_first_ms=380.000 finish_first_ms=610.000 audio_sync_first_ms=600.500 "
                  "audio_stop_ms=0.250 audio_jump_ms=0.125 audio_play_ms=600.000 finish_other_top=labels:4.000 "
                  "gpu_route_setup_first_ms=360.000"),
              split.line(3).toStdString());
}

TEST(PlaybackStartupSplit, OnlyTheFirstRequestCounts)
{
    StartupSplit split;
    ASSERT_NEAR(0.0, split.playToRequestMs(), 1e-9); // none issued: 0, never negative
    split.noteRequest(2.0);
    split.noteRequest(50.0);
    ASSERT_NEAR(2.0, split.playToRequestMs(), 1e-9);
    split.reset();
    split.noteRequest(7.0);
    ASSERT_NEAR(7.0, split.playToRequestMs(), 1e-9);
}

TEST(PlaybackStartupSplit, RouteSetupIsTheFirstPresentOverTheMedianLaterOne)
{
    StartupSplit split;
    split.noteFirstPresent(200.0, 300.0);
    for (double ms : {10.0, 30.0, 20.0, 1000.0}) split.noteLaterPresent(ms, ms); // one outlier: the median ignores it
    ASSERT_NEAR(25.0, split.medianLaterGuiPresentMs(), 1e-9);
    ASSERT_NEAR(25.0, split.medianLaterRenderMs(), 1e-9);
    ASSERT_NEAR(275.0, split.gpuRouteSetupFirstMs(), 1e-9);
    // a first present no slower than the later ones has no setup cost, never a negative one
    StartupSplit fast;
    fast.noteFirstPresent(5.0, 5.0);
    fast.noteLaterPresent(9.0, 9.0);
    ASSERT_NEAR(0.0, fast.gpuRouteSetupFirstMs(), 1e-9);
    ASSERT_NEAR(0.0, playback_startup_split::median({}), 1e-9);
    ASSERT_NEAR(3.0, playback_startup_split::median({5.0, 1.0, 3.0}), 1e-9);
}

TEST(PlaybackStartupSplit, TheTopOtherStepNeverNamesTheAudio)
{
    StartupSplit split;
    ASSERT_TRUE(split.line(1).contains(QStringLiteral(" finish_other_top=none:0.000 ")));
    split.noteFirstAudioSync(900.0); // the audio is its own field, never "other"
    split.noteFirstFinishStep(playback_startup_split::StepRecord, 2.0);
    split.noteFirstFinishStep(playback_startup_split::StepAdvance, 9.0);
    split.noteFirstFinishStep(playback_startup_split::StepSmoke, 3.0);
    ASSERT_EQ(playback_startup_split::StepAdvance, split.finishOtherTopStep());
    ASSERT_TRUE(split.line(1).contains(QStringLiteral(" finish_other_top=advance:9.000 ")));
    split.noteFirstFinishStep(playback_startup_split::StepCount, 99.0); // out of range: ignored
    ASSERT_EQ(playback_startup_split::StepAdvance, split.finishOtherTopStep());
}

TEST(PlaybackStartupSplitWiring, TheLineIsPrintedBesideStartupCatchupAndFedFromTheFirstPresent)
{
    const QString source = mainWindowSource();
    const int catchup = source.indexOf(QStringLiteral(
        "\"playback_smoke.startup_catchup session=%1 startup_wait_ms=%2 startup_catchup_frames=%3 \""));
    const int split = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.line( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int gpu = source.indexOf(QStringLiteral("\"playback_smoke.gpu_summary session=%1 cpu_frames=%2 \""));
    ASSERT_TRUE(catchup >= 0);
    ASSERT_TRUE(split > catchup);
    ASSERT_TRUE(gpu > split);
    // reset at every smoke session start, with the other first-present state
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_playbackSmokeStartupSkippedFrames = 0;\n"
        "    m_playbackStartupSplit.reset();\n")));
    // the first render request after Play
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    const uint64_t requestSerial = m_nextRenderRequestSerial++;\n"
        "    if( m_playbackSmokeActive && ui->actionPlay->isChecked() )\n"
        "        m_playbackStartupSplit.noteRequest( ( mlv_stage_timing_now() - m_playbackSmokeStartTime ) * 1000.0 );\n")));

    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    // the first present is the one before the smoke stamp counts it
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "&& m_playbackSmokePresentedFrames == 0 && !m_playbackStartupSplit.firstCaptured();")));
    // the sub-steps are marked in call order, the audio block between scopes and labels, and the smoke stamp is
    // inside the last step
    const char *order[] = {
        "recordPresentedFrame( readyFrame, requestContext );",
        "splitStep( playback_startup_split::StepRecord );",
        "splitStep( playback_startup_split::StepQuality );",
        "splitStep( playback_startup_split::StepScopes );",
        "m_pAudioPlayback->play();",
        "m_playbackStartupSplit.noteFirstAudioSync( ( t - splitMark ) * 1000.0 );",
        "drawFrameNumberLabel( static_cast<int>(displayFrame) );",
        "splitStep( playback_startup_split::StepLabels );",
        "splitStep( playback_startup_split::StepAuto );",
        "notePlayToFirstFramePresentation( displayFrame );",
        "splitStep( playback_startup_split::StepAdvance );",
        "notePlaybackSmokePresentedFrame( displayFrame, readyFrame, requestContext );",
        "emit frameReady();",
        "splitStep( playback_startup_split::StepSmoke );",
        "m_playbackStartupSplit.noteFirstFinish( ( mlv_stage_timing_now() - splitFinishEntry ) * 1000.0 );",
    };
    int at = -1;
    for (const char *needle : order)
    {
        const int next = finish.indexOf(QString::fromLatin1(needle), at + 1);
        ASSERT_TRUE(next > at);
        at = next;
    }
}
