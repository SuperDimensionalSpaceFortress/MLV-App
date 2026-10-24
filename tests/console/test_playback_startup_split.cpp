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
    // PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0: the warm witness rides at the end
    fp.warmRun = true;
    fp.setClipAtPlay = 0;
    fp.setLutsAtPlay = 1;
    ASSERT_EQ(std::string(
                  "playback_smoke.first_present_split session=7 slot=adopted handoff=direct_device_bayer16 "
                  "setup_program_ms=0.500 setup_lut_ms=1.250 setup_blur_ms=11.000 setup_realloc_ms=0.125 "
                  "setup_sampling_ms=0.250 recon_wall_ms=30.000 amaze_kernel_ms=12.500 amaze_download_ms=4.000 "
                  "paint_ms=9.000 gui_pickup_ms=40.000 backend_preloaded=1 warm_run=1 set_clip_at_play=0 "
                  "set_luts_at_play=1"
                  // PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: the probe and the kick ride at the end
                  " amaze_sm_mhz_before=NA amaze_sm_mhz_after=NA amaze_device_ms=NA clock_kick=skip "
                  "clock_kick_reason=none kick_lead_ms=NA"
                  // PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R3: the reuse fields ride at the end
                  " warm_output_reuse=skip reuse_reason=none reuse_compare_ms=0.000"),
              fp.line(7).toStdString());
}

TEST(PlaybackFirstPresentSplit, ANonWindowFirstPresentPrintsNoneAndZeros)
{
    StartupSplit split;
    ASSERT_EQ(std::string(
                  "playback_smoke.first_present_split session=2 slot=none handoff=none setup_program_ms=0.000 "
                  "setup_lut_ms=0.000 setup_blur_ms=0.000 setup_realloc_ms=0.000 setup_sampling_ms=0.000 "
                  "recon_wall_ms=0.000 amaze_kernel_ms=0.000 amaze_download_ms=0.000 paint_ms=0.000 "
                  "gui_pickup_ms=0.000 backend_preloaded=0 warm_run=0 set_clip_at_play=0 set_luts_at_play=0"
                  " amaze_sm_mhz_before=NA amaze_sm_mhz_after=NA amaze_device_ms=NA clock_kick=skip "
                  "clock_kick_reason=none kick_lead_ms=NA"
                  " warm_output_reuse=skip reuse_reason=none reuse_compare_ms=0.000"),
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

// PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0: the warm witness (warm_run, set_clip_at_play, set_luts_at_play on
// first_present_split) and playback_smoke.render_first_split from the first frame's existing stage timers.
TEST(PlaybackRenderFirstSplit, TheLineCarriesEveryFieldInOrderAndOtherIsTheRest)
{
    playback_startup_split::RenderFirstSplit rf;
    rf.renderFirstMs = 150.0;
    rf.hasDecode = true;
    rf.decodeMs = 17.0;
    rf.hasLlrawproc = true;
    rf.llrawprocMs = 120.5;
    ASSERT_NEAR(12.5, rf.otherMs(), 1e-9);
    ASSERT_EQ(std::string(
                  "playback_smoke.render_first_split session=5 render_first_ms=150.000 decode_ms=17.000 "
                  "llrawproc_ms=120.500 recon_run_ms=NA other_ms=12.500"),
              rf.line(5).toStdString());
}

TEST(PlaybackRenderFirstSplit, APartWithoutATimerIsNAAndOtherNeverGoesNegative)
{
    playback_startup_split::RenderFirstSplit rf;
    rf.renderFirstMs = 40.0;
    ASSERT_EQ(std::string(
                  "playback_smoke.render_first_split session=1 render_first_ms=40.000 decode_ms=NA llrawproc_ms=NA "
                  "recon_run_ms=NA other_ms=40.000"),
              rf.line(1).toStdString());
    rf.hasDecode = true;
    rf.decodeMs = 30.0;
    rf.hasLlrawproc = true;
    rf.llrawprocMs = 30.0;   // timers from different clocks can sum past render_first
    ASSERT_NEAR(0.0, rf.otherMs(), 1e-9);
    // a recon-run timer counts only when llrawproc (which holds the run) has none
    playback_startup_split::RenderFirstSplit run;
    run.renderFirstMs = 100.0;
    run.hasReconRun = true;
    run.reconRunMs = 60.0;
    ASSERT_NEAR(40.0, run.otherMs(), 1e-9);
    run.hasLlrawproc = true;
    run.llrawprocMs = 80.0;
    ASSERT_NEAR(20.0, run.otherMs(), 1e-9);
}

TEST(PlaybackRenderFirstSplit, ItIsResetWithTheSession)
{
    StartupSplit split;
    playback_startup_split::RenderFirstSplit rf;
    rf.renderFirstMs = 99.0;
    split.noteRenderFirstSplit(rf);
    ASSERT_NEAR(99.0, split.renderFirstSplit().renderFirstMs, 1e-9);
    ASSERT_TRUE(split.renderFirstLine(2).contains(QStringLiteral(" render_first_ms=99.000 ")));
    split.reset();
    ASSERT_NEAR(0.0, split.renderFirstSplit().renderFirstMs, 1e-9);
    ASSERT_TRUE(split.renderFirstLine(2).contains(QStringLiteral(" decode_ms=NA ")));
    // and the first-present witness resets too
    playback_startup_split::FirstPresentSplit fp;
    fp.warmRun = true;
    fp.setClipAtPlay = 3;
    split.noteFirstPresentSplit(fp);
    ASSERT_TRUE(split.firstPresentLine(1).contains(QStringLiteral(" warm_run=1 set_clip_at_play=3 set_luts_at_play=0")));
    split.reset();
    ASSERT_TRUE(split.firstPresentLine(1).contains(QStringLiteral(" warm_run=0 set_clip_at_play=0 set_luts_at_play=0")));
}

TEST(PlaybackRenderFirstSplitWiring, FedFromTheFirstPresentAndPrintedAfterTheFirstPresentLine)
{
    const QString source = mainWindowSource();
    const int firstPresent = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.firstPresentLine( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int renderFirst = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.renderFirstLine( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int gpu = source.indexOf(QStringLiteral("\"playback_smoke.gpu_summary session=%1 cpu_frames=%2 \""));
    ASSERT_TRUE(firstPresent >= 0);
    ASSERT_TRUE(renderFirst > firstPresent);
    ASSERT_TRUE(gpu > renderFirst);
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    const int splitFirst = finish.indexOf(QStringLiteral("    if( splitFirst )\n"));
    const int fpFed = finish.indexOf(QStringLiteral("m_playbackStartupSplit.noteFirstPresentSplit( fp );"));
    const int rfFed = finish.indexOf(QStringLiteral("m_playbackStartupSplit.noteRenderFirstSplit( rf );"));
    ASSERT_TRUE(splitFirst >= 0);
    ASSERT_TRUE(fpFed > splitFirst);
    ASSERT_TRUE(rfFed > fpFed);
    const QString feed = finish.mid(splitFirst, rfFed - splitFirst);
    // the witness: the session's set_clip / set_luts calls up to the first present, from the session-start baselines
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.warmRun = m_playbackReconWarmRan;")));
    ASSERT_TRUE(feed.contains(QStringLiteral(
        "fp.setClipAtPlay = static_cast<long long>( llrpGpuPlaybackReconSetClipCount() - m_playbackSmokeStartGpuSetClipCount );")));
    ASSERT_TRUE(feed.contains(QStringLiteral(
        "fp.setLutsAtPlay = static_cast<long long>( llrpGpuPlaybackReconSetLutsCount() - m_playbackSmokeStartGpuSetLutsCount );")));
    // existing timers only: render_first is the startup_split's own, the parts are keys the frame already carries
    ASSERT_TRUE(feed.contains(QStringLiteral("rf.renderFirstMs = telemetryDoubleValue( t, \"render_thread_total_ms\" );")));
    ASSERT_TRUE(feed.contains(QStringLiteral("rf.hasDecode = t.contains( QStringLiteral( \"raw_uint16_ms\" ) );")));
    ASSERT_TRUE(feed.contains(QStringLiteral("rf.hasLlrawproc = t.contains( QStringLiteral( \"llrawproc_total_ms\" ) );")));
    ASSERT_FALSE(feed.contains(QStringLiteral("mlv_stage_timing_now")));
    ASSERT_FALSE(feed.contains(QStringLiteral("QElapsedTimer")));
    // the baselines are taken at the smoke session start
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_playbackSmokeStartGpuSetClipCount = llrpGpuPlaybackReconSetClipCount();\n"
        "    m_playbackSmokeStartGpuSetLutsCount = llrpGpuPlaybackReconSetLutsCount();\n")));
}

// PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: the first Play AMaZE's in-process probe (SM MHz before / after its
// tile batch, the batch's device time) and the Play-start kick on first_present_split; the session's first four AMaZE
// runs on playback_smoke.amaze_first_frames; the same probe fields on the prewarm and warm lines. NA whenever the DLL
// lacks the probe symbol or the run was not probed.
TEST(PlaybackAmazeProbeFields, KnownOnlyWithTheSymbolAndAValidProbe)
{
    using playback_startup_split::amazeProbeFields;
    ASSERT_EQ(std::string(" amaze_sm_mhz_before=210.4 amaze_sm_mhz_after=1755.0 amaze_device_ms=131.250"),
              amazeProbeFields(true, true, 210.42, 1755.0, 131.25).toStdString());
    const std::string na(" amaze_sm_mhz_before=NA amaze_sm_mhz_after=NA amaze_device_ms=NA");
    ASSERT_EQ(na, amazeProbeFields(false, true, 210.0, 1755.0, 131.25).toStdString());   // an older DLL
    ASSERT_EQ(na, amazeProbeFields(true, false, 210.0, 1755.0, 131.25).toStdString());   // unprobed or failed
}

TEST(PlaybackAmazeProbeFields, TheFirstPresentSplitCarriesTheProbeAndTheKick)
{
    playback_startup_split::FirstPresentSplit fp;
    fp.probeAvailable = true;
    fp.probeValid = true;
    fp.amazeSmMhzBefore = 1650.0;
    fp.amazeSmMhzAfter = 1800.0;
    fp.amazeDeviceMs = 17.5;
    fp.clockKickRun = true;
    fp.clockKickReason = QStringLiteral("none");
    fp.kickLeadMs = 190.25;
    const std::string text = fp.line(9).toStdString();
    ASSERT_TRUE(text.find(" set_luts_at_play=0 amaze_sm_mhz_before=1650.0 amaze_sm_mhz_after=1800.0 "
                          "amaze_device_ms=17.500 clock_kick=run clock_kick_reason=none kick_lead_ms=190.250")
                != std::string::npos);
    // the kick lead is followed only by the reuse fields (PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R3)
    const std::string tail = "kick_lead_ms=190.250 warm_output_reuse=skip reuse_reason=none reuse_compare_ms=0.000";
    ASSERT_EQ(text.size() - tail.size(), text.rfind(tail));
    // a skipped kick names why; a probe the DLL cannot give is NA
    fp.probeAvailable = false;
    fp.clockKickRun = false;
    fp.clockKickReason = QStringLiteral("cpu_route");
    fp.kickLeadMs = -1.0;
    ASSERT_TRUE(fp.line(9).contains(QStringLiteral(
        " amaze_sm_mhz_before=NA amaze_sm_mhz_after=NA amaze_device_ms=NA clock_kick=skip clock_kick_reason=cpu_route "
        "kick_lead_ms=NA")));
    fp.clockKickReason.clear();
    ASSERT_TRUE(fp.line(9).contains(QStringLiteral(" clock_kick_reason=none ")));
}

TEST(PlaybackAmazeFirstFrames, EachRunIsKernelDeviceMhzAndARunNotReachedIsNA)
{
    playback_startup_split::AmazeFirstFrames frames;
    ASSERT_EQ(std::string("playback_smoke.amaze_first_frames session=4 f0=NA/NA/NA f1=NA/NA/NA f2=NA/NA/NA f3=NA/NA/NA"),
              frames.line(4).toStdString());
    frames.count = 3;
    frames.probeAvailable = true;
    frames.kernelMs[0] = 144.125;
    frames.deviceMs[0] = 140.5;
    frames.smMhzBefore[0] = 210.0;
    frames.valid[0] = true;
    frames.kernelMs[1] = 19.0;
    frames.deviceMs[1] = 17.25;
    frames.smMhzBefore[1] = 1755.0;
    frames.valid[1] = true;
    frames.kernelMs[2] = 18.5;   // a probe step failed on this run: its kernel_ms is still known
    frames.valid[2] = false;
    ASSERT_EQ(std::string("playback_smoke.amaze_first_frames session=5 f0=144.125/140.500/210.0 f1=19.000/17.250/1755.0 "
                          "f2=18.500/NA/NA f3=NA/NA/NA"),
              frames.line(5).toStdString());
    // an older DLL: kernel_ms only
    frames.probeAvailable = false;
    ASSERT_TRUE(frames.line(5).contains(QStringLiteral(" f0=144.125/NA/NA f1=19.000/NA/NA ")));
}

TEST(PlaybackAmazeFirstFrames, ItIsResetWithTheSession)
{
    StartupSplit split;
    playback_startup_split::AmazeFirstFrames frames;
    frames.count = 1;
    frames.kernelMs[0] = 20.0;
    split.noteAmazeFirstFrames(frames);
    ASSERT_EQ(1, split.amazeFirstFrames().count);
    ASSERT_TRUE(split.amazeFirstFramesLine(2).contains(QStringLiteral(" f0=20.000/NA/NA ")));
    split.reset();
    ASSERT_EQ(0, split.amazeFirstFrames().count);
    ASSERT_TRUE(split.amazeFirstFramesLine(2).contains(QStringLiteral(" f0=NA/NA/NA ")));
}

TEST(PlaybackAmazeProbeWiring, FedFromTheSessionRecordAndPrintedAfterTheRenderFirstLine)
{
    const QString source = mainWindowSource();
    const int renderFirst = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.renderFirstLine( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int firstFrames = source.indexOf(QStringLiteral(
        "qInfo().noquote() << m_playbackStartupSplit.amazeFirstFramesLine( static_cast<qulonglong>( m_playbackSmokeSessionId ) );"));
    const int gpu = source.indexOf(QStringLiteral("\"playback_smoke.gpu_summary session=%1 cpu_frames=%2 \""));
    ASSERT_TRUE(renderFirst >= 0);
    ASSERT_TRUE(firstFrames > renderFirst);
    ASSERT_TRUE(gpu > firstFrames);
    const QString feedFrames = source.mid(renderFirst, firstFrames - renderFirst);
    ASSERT_TRUE(feedFrames.contains(QStringLiteral("const GpuAmazeProbeSessionRecord record = gpuAmazeDebayerProbeSessionRecord();")));
    ASSERT_TRUE(feedFrames.contains(QStringLiteral("m_playbackStartupSplit.noteAmazeFirstFrames( frames );")));
    // first_present_split: frame 0 is the session record's first probed run, beside the kick decision
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    const int splitFirst = finish.indexOf(QStringLiteral("    if( splitFirst )\n"));
    const int record = finish.indexOf(QStringLiteral(
        "const GpuAmazeProbeSessionRecord probeRecord = gpuAmazeDebayerProbeSessionRecord();"));
    const int fed = finish.indexOf(QStringLiteral("m_playbackStartupSplit.noteFirstPresentSplit( fp );"));
    ASSERT_TRUE(splitFirst >= 0);
    ASSERT_TRUE(record > splitFirst);
    ASSERT_TRUE(fed > record);
    const QString feed = finish.mid(record, fed - record);
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.amazeSmMhzBefore = probeRecord.smMhzBefore[0];")));
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.amazeDeviceMs = probeRecord.deviceMs[0];")));
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.kickLeadMs = probeRecord.startSteadyMs[0] - m_playbackGpuClockKickBeginSteadyMs;")));
    ASSERT_TRUE(feed.contains(QStringLiteral("fp.clockKickRun = m_playbackGpuClockKickDecision.run;")));
    // the probe fields on the prewarm line (GpuDisplayWindow) and the warm line (MainWindow)
    const QString windowPath = repo_file_path(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    QFile windowFile(windowPath);
    ASSERT_TRUE(windowFile.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString window = QTextStream(&windowFile).readAll();
    const QString prewarm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"),
                                        QStringLiteral("GpuPresentSetupTiming GpuDisplayWindow::lastPresentSetupTimingIfActive()"));
    ASSERT_FALSE(prewarm.isEmpty());
    const int arm = prewarm.indexOf(QStringLiteral("gpuAmazeDebayerArmProbe(1);"));
    const int run = prewarm.indexOf(QStringLiteral("gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(warmInput->textureId(),"));
    const int disarm = prewarm.indexOf(QStringLiteral("gpuAmazeDebayerArmProbe(0);"));
    ASSERT_TRUE(arm >= 0);
    ASSERT_TRUE(run > arm);
    ASSERT_TRUE(disarm > run);
    ASSERT_TRUE(prewarm.contains(QStringLiteral("*summary += playback_startup_split::amazeProbeFields(amazeTiming.probeAvailable,")));
    const QString warmDone = bodyBetween(source, QStringLiteral("void MainWindow::onPlaybackReconWarmDone("),
                                         QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"));
    ASSERT_TRUE(warmDone.contains(QStringLiteral("+ playback_startup_split::amazeProbeFields( amazeTiming.probeAvailable, amazeTiming.probeValid,")));
}
