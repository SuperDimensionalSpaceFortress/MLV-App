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

// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row 0: playback_smoke.first_present_split -- where the first GPU
// window present itself went (slot outcome, setup parts, recon, AMaZE kernel/download, paint, GUI pickup).
TEST(PlaybackFirstPresentSplit, TheLineCarriesEveryFieldInOrder)
{
    playback_startup_split::FirstPresentSplit fp;
    fp.slot = QStringLiteral("adopted");
    fp.handoff = QStringLiteral("direct_device_bayer16");
    fp.setupProgramMs = 0.5;
    fp.setupLutMs = 1.25;
    fp.setupBlurMs = 11.0;
    fp.setupReallocMs = 0.125;
    fp.setupSamplingMs = 0.25;
    fp.reconWallMs = 30.0;
    fp.amazeKernelMs = 12.5;
    fp.amazeDownloadMs = 4.0;
    fp.paintMs = 9.0;
    fp.guiPickupMs = 40.0;
    fp.backendPreloaded = true;
    ASSERT_EQ(std::string(
                  "playback_smoke.first_present_split session=7 slot=adopted handoff=direct_device_bayer16 "
                  "setup_program_ms=0.500 setup_lut_ms=1.250 setup_blur_ms=11.000 setup_realloc_ms=0.125 "
                  "setup_sampling_ms=0.250 recon_wall_ms=30.000 amaze_kernel_ms=12.500 amaze_download_ms=4.000 "
                  "paint_ms=9.000 gui_pickup_ms=40.000 backend_preloaded=1"),
              fp.line(7).toStdString());
}

TEST(PlaybackFirstPresentSplit, ANonWindowFirstPresentPrintsNoneAndZeros)
{
    StartupSplit split;
    ASSERT_EQ(std::string(
                  "playback_smoke.first_present_split session=2 slot=none handoff=none setup_program_ms=0.000 "
                  "setup_lut_ms=0.000 setup_blur_ms=0.000 setup_realloc_ms=0.000 setup_sampling_ms=0.000 "
                  "recon_wall_ms=0.000 amaze_kernel_ms=0.000 amaze_download_ms=0.000 paint_ms=0.000 "
                  "gui_pickup_ms=0.000 backend_preloaded=0"),
              split.firstPresentLine(2).toStdString());
    // empty free text never leaves a field without a value
    playback_startup_split::FirstPresentSplit fp;
    fp.slot.clear();
    fp.handoff.clear();
    ASSERT_TRUE(fp.line(1).contains(QStringLiteral(" slot=none handoff=none ")));
    // a '%' in free text is printed as is, never substituted
    fp.handoff = QStringLiteral("odd%1mode");
    ASSERT_TRUE(fp.line(1).contains(QStringLiteral(" handoff=odd%1mode ")));
}

TEST(PlaybackFirstPresentSplit, ItIsResetWithTheSessionAndKeptUntilThen)
{
    StartupSplit split;
    playback_startup_split::FirstPresentSplit fp;
    fp.slot = QStringLiteral("realloc");
    fp.reconWallMs = 3.0;
    split.noteFirstPresentSplit(fp);
    ASSERT_EQ(std::string("realloc"), split.firstPresentSplit().slot.toStdString());
    ASSERT_TRUE(split.firstPresentLine(4).contains(QStringLiteral(" recon_wall_ms=3.000 ")));
    split.reset();
    ASSERT_EQ(std::string("none"), split.firstPresentSplit().slot.toStdString());
    ASSERT_NEAR(0.0, split.firstPresentSplit().reconWallMs, 1e-9);
}

TEST(PlaybackFirstPresentSplit, GuiPickupIsRenderDoneToGuiPresentStart)
{
    using playback_startup_split::guiPickupMs;
    ASSERT_NEAR(40.0, guiPickupMs(10.000, 10.040), 1e-6);
    ASSERT_NEAR(0.0, guiPickupMs(0.0, 10.040), 1e-9);   // render stamp unknown
    ASSERT_NEAR(0.0, guiPickupMs(10.050, 10.040), 1e-9); // out of order: never negative
}

TEST(PlaybackFirstPresentSplitWiring, PrintedAfterStartupSplitAndFedFromTheFirstPresentsTelemetry)
{
    const QString source = mainWindowSource();
    const int split = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.line( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int firstPresent = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.firstPresentLine( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int gpu = source.indexOf(QStringLiteral("\"playback_smoke.gpu_summary session=%1 cpu_frames=%2 \""));
    ASSERT_TRUE(split >= 0);
    ASSERT_TRUE(firstPresent > split);
    ASSERT_TRUE(gpu > firstPresent);
    // the window's setup split of the first call carries the slot outcome and the AMaZE-only parts
    ASSERT_TRUE(source.contains(QStringLiteral(
        "QStringLiteral(\"gpu_playback_recon_texture_present_slot\"),\n"
        "                QString::fromLatin1( st.slot ? st.slot : \"none\" ) );")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "QStringLiteral(\"gpu_playback_recon_texture_present_amaze_download_ms\"), st.amaze_download_ms );")));
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    const int firstNote = finish.indexOf(QStringLiteral("m_playbackStartupSplit.noteFirstPresent( splitRenderMs, splitGuiPresentMs );"));
    const int fed = finish.indexOf(QStringLiteral("m_playbackStartupSplit.noteFirstPresentSplit( fp );"));
    ASSERT_TRUE(firstNote >= 0);
    ASSERT_TRUE(fed > firstNote);
    const QString feed = finish.mid(firstNote, fed - firstNote);
    ASSERT_TRUE(feed.contains(QStringLiteral("    if( splitFirst )\n")));
    ASSERT_TRUE(feed.contains(QStringLiteral(
        "fp.guiPickupMs = playback_startup_split::guiPickupMs( readyFrame.frameReadyEmitStageTime, displayStart );")));
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.backendPreloaded = m_playbackReconBackendPreloaded;")));
    ASSERT_TRUE(feed.contains(QStringLiteral("\"gpu_playback_recon_texture_present_post_ms\"")));
    // and the window records both parts on every present
    const QString windowPath = repo_file_path(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    QFile windowFile(windowPath);
    ASSERT_TRUE(windowFile.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString window = QTextStream(&windowFile).readAll();
    ASSERT_TRUE(window.contains(QStringLiteral("setupTiming.amaze_download_ms = amazeTiming.downloadMs;")));
    ASSERT_TRUE(window.contains(QStringLiteral("setupTiming.slot = \"kept\";")));
}
