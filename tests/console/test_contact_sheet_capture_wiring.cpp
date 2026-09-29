// Wiring/census test for CUDA-PLAYBACK-CONTACT-SHEET-1's --contact-sheet-dir/
// --contact-sheet-frames option: pins (1) the option defaults to OFF and the capture block is
// gated on both fields being set, so an off run performs zero grabs and zero readbacks, (2) the
// capture block runs strictly AFTER playback is stopped (ui->actionPlay->setChecked( false ))
// and its idle-drain wait, which is also strictly after the timed while() loop that drives
// fps/frame-count measurement closes -- so N contact-sheet grabs never land inside the measured
// playback interval or perturb its telemetry, and (3) the CLI wires --contact-sheet-dir/
// --contact-sheet-frames together (main.cpp) with the option struct's defaults reproduced there.
// MainWindow.cpp/main.cpp need a full GUI build (not linked into console_tests), so this test
// reads the sources as text, mirroring test_gpu_window_swap_wiring.cpp's and
// test_playback_smoke_foreground_wiring.cpp's approach for the same reason.
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

QString sliceBetween(const QString & source, const QString & fromMarker, const QString & toMarker)
{
    const int at = source.indexOf(fromMarker);
    if (at < 0) return QString();
    const int next = source.indexOf(toMarker, at);
    if (next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(ContactSheetCaptureWiring, OptionStructDefaultsToOff)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_FALSE(optionsStruct.isEmpty());
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("QString contactSheetDir;")));
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("int contactSheetFrames = 0;")));
}

TEST(ContactSheetCaptureWiring, CliDefaultsToOffAndRequiresBothFlagsTogether)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-dir\")")));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-frames\")")));
    // The frames option's own declared default is "0" (off); a caller who never passes either
    // flag gets an options.contactSheetFrames of 0 and an empty options.contactSheetDir.
    const QString framesOptBlock = sliceBetween(source,
        QStringLiteral("contactSheetFramesOpt("),
        QStringLiteral("parser.addOption(contactSheetFramesOpt)"));
    ASSERT_FALSE(framesOptBlock.isEmpty());
    ASSERT_TRUE(framesOptBlock.contains(QStringLiteral("QStringLiteral(\"0\")")));

    // Both-or-neither: -dir set XOR frames > 0 is refused before either option struct field is
    // ever populated, so a caller can never end up with a directory and no frame count or vice
    // versa reaching runGuiPlaybackSmoke().
    ASSERT_TRUE(source.contains(
        QStringLiteral("if (contactSheetDirSet != (contactSheetFrames > 0))")));
}

TEST(ContactSheetCaptureWiring, CaptureBlockIsGatedOnBothDirAndFramesBeingSet)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )")));
    // The per-frame grab loop is bounded by options.contactSheetFrames, and nowhere else in the
    // capture block hard-codes a frame count -- an off run (frames == 0) executes the loop body
    // zero times, performing zero grabPresentedFramebufferIfActive() readbacks.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("for( int i = 0; i < options.contactSheetFrames; ++i )")));
    const int guardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    const int loopAt = smokeBody.indexOf(
        QStringLiteral("for( int i = 0; i < options.contactSheetFrames; ++i )"), guardAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(loopAt > guardAt);
}

TEST(ContactSheetCaptureWiring, CaptureRunsStrictlyAfterPlaybackStopsAndAfterTheTimedLoopCloses)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("while( playbackClock.elapsed() < durationMs && ui->actionPlay->isChecked() )"));
    const int stopPlaybackAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    const int idleDrainAt = smokeBody.indexOf(
        QStringLiteral("for( int attempt = 0; attempt < 400 && m_pRenderThread && !m_pRenderThread->isIdle(); ++attempt )"),
        stopPlaybackAt);
    const int captureGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    const int doneLineAt = smokeBody.indexOf(QStringLiteral("out << \"[GUI-SMOKE] DONE"));

    ASSERT_TRUE(timedLoopAt >= 0);
    ASSERT_TRUE(stopPlaybackAt > timedLoopAt);
    ASSERT_TRUE(idleDrainAt > stopPlaybackAt);
    ASSERT_TRUE(captureGuardAt > idleDrainAt);
    ASSERT_TRUE(doneLineAt > captureGuardAt);

    // The capture block's own grabs must never be attributed to the playback_smoke frame
    // counters or the GPU swap-telemetry session that the measured interval's fps/swap-cadence
    // numbers are computed from: finishPlaybackSmokeTelemetry() runs synchronously inside
    // on_actionPlay_toggled(false), which setChecked(false) above triggers before this test's
    // captureGuardAt position, and it is the one place m_playbackSmokeActive is cleared and the
    // swap-telemetry session is closed.
    const QString togglePlayBody = sliceBetween(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(togglePlayBody.isEmpty());
    ASSERT_TRUE(togglePlayBody.contains(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );")));
    const int checkedFalseBranchAt = togglePlayBody.indexOf(QStringLiteral("if( !checked )"));
    const int finishCallAt = togglePlayBody.indexOf(
        QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"), checkedFalseBranchAt);
    ASSERT_TRUE(checkedFalseBranchAt >= 0);
    ASSERT_TRUE(finishCallAt > checkedFalseBranchAt);
}

TEST(ContactSheetCaptureWiring, EachSidecarCarriesTheFieldsTheComposerAndOwnerNeed)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    static const char * kRequiredFields[] = {
        "index", "captured_utc", "serial", "display_frame", "elapsed_ms",
        "texture_source", "path", "look_assist_enabled", "look_assist_scene",
        "look_assist_exposure", "look_assist_contrast", "look_assist_pivot",
        "look_assist_temperature", "look_assist_tint", "settled", "saved",
    };
    for (const char * field : kRequiredFields) {
        const QString needle = QStringLiteral("QStringLiteral(\"%1\")").arg(QString::fromLatin1(field));
        ASSERT_TRUE(smokeBody.contains(needle));
    }
}

TEST(ContactSheetCaptureWiring, SeekModeDefaultsToOffSoPlaybackPassCaptureIsTheDefault)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_FALSE(optionsStruct.isEmpty());
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("bool contactSheetSeekMode = false;")));
}

TEST(ContactSheetCaptureWiring, CliWiresTheSeekModeFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-seek-mode\")")));
    ASSERT_TRUE(source.contains(
        QStringLiteral("options.contactSheetSeekMode = parser.isSet(contactSheetSeekModeOpt);")));
}

TEST(ContactSheetCaptureWiring, PlaybackModeRestartsPlaybackOnlyAfterTheMeasuredIntervalAndPlaybackStopAndIdleDrain)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("while( playbackClock.elapsed() < durationMs && ui->actionPlay->isChecked() )"));
    const int stopPlaybackAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    const int idleDrainAt = smokeBody.indexOf(
        QStringLiteral("for( int attempt = 0; attempt < 400 && m_pRenderThread && !m_pRenderThread->isIdle(); ++attempt )"),
        stopPlaybackAt);
    const int captureActiveAt = smokeBody.indexOf(QStringLiteral("m_contactSheetCaptureActive = true;"));
    const int restartPlayAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->trigger();"), captureActiveAt);

    ASSERT_TRUE(timedLoopAt >= 0);
    ASSERT_TRUE(stopPlaybackAt > timedLoopAt);
    ASSERT_TRUE(idleDrainAt > stopPlaybackAt);
    // The playback-mode branch (which sets m_contactSheetCaptureActive and then restarts
    // playback) lives inside the same capture guard as the seek-mode branch, so it too must
    // sit strictly after the measured interval's stop + idle-drain -- never inside it.
    ASSERT_TRUE(captureActiveAt > idleDrainAt);
    ASSERT_TRUE(restartPlayAt > captureActiveAt);
}

TEST(ContactSheetCaptureWiring, PerPresentedFrameHookIsCalledFromTheSameSiteAsPlaybackSmokeTelemetryAndGatedOnItsOwnFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // Declared next to notePlaybackSmokePresentedFrame and called from the same real-
    // presented-frame call site (finishPresentedFrame), shortly after it -- so every
    // playback-mode contact-sheet grab is of a frame actually presented during playback,
    // the same event that drives the playback_smoke fps/frame counters.
    const int noteCallAt = source.indexOf(
        QStringLiteral("notePlaybackSmokePresentedFrame( displayFrame, readyFrame, requestContext );"));
    const int hookCallAt = source.indexOf(
        QStringLiteral("noteContactSheetPresentedFrame( displayFrame, readyFrame, requestContext );"));
    ASSERT_TRUE(noteCallAt >= 0);
    ASSERT_TRUE(hookCallAt > noteCallAt);
    ASSERT_TRUE(hookCallAt - noteCallAt < 700);

    // HARDENING (default-off, CUDA-PLAYBACK-CONTACT-SHEET-2): the call itself -- not just the
    // hook's own early return -- must be gated on a cheap member bool set only when the
    // contact-sheet options are actually present, so a smoke run with the options off makes
    // zero noteContactSheetPresentedFrame() calls on the measured-frame hot path.
    const int callGuardAt = source.lastIndexOf(
        QStringLiteral("if( m_contactSheetOptionsPresent )"), hookCallAt);
    ASSERT_TRUE(callGuardAt >= 0);
    ASSERT_TRUE(callGuardAt < hookCallAt);
    ASSERT_TRUE(hookCallAt - callGuardAt < 100);

    // Gated on its own flag, never on m_playbackSmokeActive -- it must keep working (and keep
    // counting) after the measured interval's playback_smoke session has already closed.
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("if( !m_contactSheetCaptureActive ) return;")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("m_playbackSmokeActive")));
}

TEST(ContactSheetCaptureWiring, DefaultOffOptionsPresentFlagIsSetOnlyInsideTheContactSheetBlock)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_contactSheetOptionsPresent = false;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int guardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    const int setTrueAt = smokeBody.indexOf(
        QStringLiteral("m_contactSheetOptionsPresent = true;"), guardAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(setTrueAt > guardAt);
    ASSERT_TRUE(setTrueAt - guardAt < 600);

    // Set true exactly once in the whole file, and only inside this block -- an unconditional
    // or earlier assignment would defeat the whole point of gating the per-present call on it.
    ASSERT_EQ(1, source.count(QStringLiteral("m_contactSheetOptionsPresent = true;")));
}

TEST(ContactSheetCaptureWiring, PlaybackModeSidecarRecordsRenderPathAndPlaybackPathTrue)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"render_path\")")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), true );")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("mainWindowGpuPlaybackPipelineStatusToken( contactFramePipelineStatus )")));
}

TEST(ContactSheetCaptureWiring, SeekModeSidecarRecordsPlaybackPathFalse)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    // Not sliceBetween(..., "else"): the seek branch's own body contains several inner
    // if/else fallback chains, whose first "else" would truncate the slice long before the
    // sidecar-writing code below it. Slice to the playback-mode branch's own distinctive
    // first statement instead, which only appears after the seek branch's closing brace.
    const QString seekBranch = sliceBetween(smokeBody,
        QStringLiteral("if( options.contactSheetSeekMode )"),
        QStringLiteral("m_contactSheetCaptureDir = options.contactSheetDir;"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), false );")));
}

TEST(ContactSheetCaptureWiring, GpuWindowReadbackIsTriedBeforeAnyCpuPathFallback)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const int guardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    ASSERT_TRUE(guardAt >= 0);
    const QString captureBlock = smokeBody.mid(guardAt);
    const int gpuActiveAt = captureBlock.indexOf(QStringLiteral("GpuDisplayWindow::isActive()"));
    const int gpuReadbackAt = captureBlock.indexOf(
        QStringLiteral("GpuDisplayWindow::grabPresentedFramebufferIfActive("));
    const int viewportFallbackAt = captureBlock.indexOf(
        QStringLiteral("app_internal_gl_viewport_grab"));
    const int pixmapFallbackAt = captureBlock.indexOf(
        QStringLiteral("app_internal_presented_pixmap"));
    ASSERT_TRUE(gpuActiveAt >= 0);
    ASSERT_TRUE(gpuReadbackAt > gpuActiveAt);
    ASSERT_TRUE(viewportFallbackAt > gpuReadbackAt);
    ASSERT_TRUE(pixmapFallbackAt > viewportFallbackAt);
}

// --- r1c pre-review fixes (sol, prereview-cs1-20260926T0315Z) --------------------------------

TEST(ContactSheetCaptureWiring, B1_HookDisarmsOnAnyFrameNotPresentedWhilePlayIsChecked)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());

    // The restart re-cue's own seek can present a frame while m_contactSheetCaptureActive is
    // already true (armed before ui->actionPlay->trigger() ever runs) -- the hook must refuse
    // to capture that frame, strictly BEFORE it ever reaches the save/frameJson logic below.
    const int activeGuardAt = hookBody.indexOf(QStringLiteral("if( !m_contactSheetCaptureActive ) return;"));
    const int checkedGuardAt = hookBody.indexOf(QStringLiteral("if( !ui->actionPlay->isChecked() ) return;"));
    const int saveAt = hookBody.indexOf(QStringLiteral(".save( pngPath, \"PNG\" )"));
    ASSERT_TRUE(activeGuardAt >= 0);
    ASSERT_TRUE(checkedGuardAt > activeGuardAt);
    ASSERT_TRUE(saveAt > checkedGuardAt);
}

TEST(ContactSheetCaptureWiring, B2_RestartNeverReArmsPlaybackSmokeTelemetryOrSwapCounters)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString togglePlayBody = sliceBetween(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(togglePlayBody.isEmpty());

    // beginPlaybackSmokeTelemetry() (which also resets the GPU swap-telemetry session) must be
    // reachable only when a contact-sheet capture pass is NOT the one re-triggering Play --
    // otherwise every capture restart would open a new session and emit its own
    // playback_smoke.frame/summary/gpu_summary lines the Bachelor job's parsers could pick up.
    const int checkedBranchAt = togglePlayBody.indexOf(QStringLiteral("if( checked )"));
    ASSERT_TRUE(checkedBranchAt >= 0);
    const int guardAt = togglePlayBody.indexOf(
        QStringLiteral("if( !m_contactSheetCaptureActive ) beginPlaybackSmokeTelemetry();"), checkedBranchAt);
    ASSERT_TRUE(guardAt > checkedBranchAt);
    // An unguarded call must not also remain (which would defeat the guard above).
    const int bareCallAt = togglePlayBody.indexOf(QStringLiteral("\n        beginPlaybackSmokeTelemetry();\n"));
    ASSERT_TRUE(bareCallAt < 0);
}

TEST(ContactSheetCaptureWiring, H1_RestartSeekIsBoundedByTheRemainingOverallDeadline)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // seekAndSettleLoadedClip gained an optional timeoutMs parameter (default 8000, so every
    // OTHER call site -- the stress seeks, the seek-mode capture -- is unaffected).
    const int lambdaAt = smokeBody.indexOf(QStringLiteral("auto seekAndSettleLoadedClip = [&]("));
    ASSERT_TRUE(lambdaAt >= 0);
    const QString lambdaHeader = smokeBody.mid(lambdaAt, 400);
    ASSERT_TRUE(lambdaHeader.contains(QStringLiteral("int timeoutMs = 8000")));

    // The restart call site passes what remains of contactSheetPlaybackTimeoutMs, clamped to
    // [1, 8000] -- never its own fixed 8000ms regardless of how much of the pass is left.
    const int restartsAt = smokeBody.indexOf(QStringLiteral("const int contactSheetMaxRestarts = 50;"));
    ASSERT_TRUE(restartsAt >= 0);
    const int restartTimeoutAt = smokeBody.indexOf(
        QStringLiteral("const qint64 restartSeekTimeoutMs = qBound("), restartsAt);
    ASSERT_TRUE(restartTimeoutAt > restartsAt);
    const int restartSeekCallAt = smokeBody.indexOf(
        QStringLiteral("seekAndSettleLoadedClip(\n                        sheetStartFrame, \"gui-smoke-contact-sheet-restart\","),
        restartTimeoutAt);
    ASSERT_TRUE(restartSeekCallAt > restartTimeoutAt);
    ASSERT_TRUE(smokeBody.indexOf(QStringLiteral("restartSeekTimeoutMs"), restartSeekCallAt) > restartSeekCallAt);
}

TEST(ContactSheetCaptureWiring, H2_GpuWindowGrabFailureOrSerialMismatchFailsClosedRatherThanFallingBack)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());

    ASSERT_TRUE(hookBody.contains(QStringLiteral("bool gpuWindowGrabFailedClosed = false;")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("grabbedSerialValid && grabbedSerial != readyFrame.requestSerial")));
    // Every fallback below the GPU-window readback must be skipped once that leg has failed
    // closed -- a failed/mismatched GPU-window grab must never be silently replaced by a
    // viewport/pixmap grab still labelled playback_path=true.
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull()\n"
                        "     && ( GpuDisplayViewport::isTexturePresentationActive( ui->graphicsView )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull() && m_pGraphicsItem )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull()\n"
                        "     && ui->graphicsView && ui->graphicsView->viewport() )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("bool frameOk = !gpuWindowGrabFailedClosed\n")));
}

TEST(ContactSheetCaptureWiring, H3_SidecarPathFieldIsRelativeNeverAbsolute)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngRelativeName );")));
    ASSERT_FALSE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngPath );")));

    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const QString seekBranch = sliceBetween(smokeBody,
        QStringLiteral("if( options.contactSheetSeekMode )"),
        QStringLiteral("m_contactSheetCaptureDir = options.contactSheetDir;"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngRelativeName );")));
    ASSERT_FALSE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngPath );")));
}

TEST(ContactSheetCaptureWiring, B4_DoneLineOmitsFramesWrittenFieldEntirelyWhenOptionsAreOff)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int doneLineAt = smokeBody.indexOf(QStringLiteral("out << \"[GUI-SMOKE] DONE"));
    ASSERT_TRUE(doneLineAt >= 0);
    const int fieldGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )\n"
                        "        out << \" contact_sheet_frames_written=\" << contactSheetFramesWritten;"),
        doneLineAt);
    ASSERT_TRUE(fieldGuardAt > doneLineAt);
    // The unconditional field append must not also remain right on the DONE out<< chain.
    ASSERT_FALSE(smokeBody.contains(
        QStringLiteral("<< \" diagnostic_log_file=\" << CrashForensics::currentLogFilePath()\n"
                        "        << \" contact_sheet_frames_written=\"")));
}

// --- r1d pre-review #2 finding (sol, prereview-cs1b-sol-20260926T0450Z) ----------------------

TEST(ContactSheetCaptureWiring, FinishTelemetryClearsFrameAndTimelineFlagsAfterEverySummaryReadsThem)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString finishBody = sliceBetween(source,
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
    ASSERT_FALSE(finishBody.isEmpty());

    // HARDENING fix: a capture-playback frame emitted after the measured session closes (e.g.
    // a contact-sheet capture pass's frames, drawn via beginPlaybackSmokeTelemetry-suppressed
    // restarts) must stop passing the m_playbackSmokeFrameTelemetry/
    // m_playbackSmokeTimelineTelemetry gates elsewhere -- otherwise it is logged (e.g.
    // playback_auto.decision) carrying this now-closed session's id.
    const int clearFrameAt = finishBody.indexOf(QStringLiteral("m_playbackSmokeFrameTelemetry = false;"));
    const int clearTimelineAt = finishBody.indexOf(QStringLiteral("m_playbackSmokeTimelineTelemetry = false;"));
    ASSERT_TRUE(clearFrameAt >= 0);
    ASSERT_TRUE(clearTimelineAt >= 0);

    // Cleared LAST: every read of either flag inside this same function (the
    // playback_smoke.summary line's frame_telemetry field and the playback_smoke.foreground
    // line's telemetry_enabled field) must see the session's TRUE value, not a value this same
    // call already zeroed -- clearing earlier would make this session's own summary misreport
    // a telemetry-enabled session as disabled.
    const int summaryFrameTelemetryReadAt = finishBody.indexOf(
        QStringLiteral(".arg( bool01( m_playbackSmokeFrameTelemetry ) )"));
    const int foregroundGuardAt = finishBody.indexOf(QStringLiteral("if ( m_playbackSmokeFrameTelemetry )"));
    ASSERT_TRUE(summaryFrameTelemetryReadAt >= 0);
    ASSERT_TRUE(foregroundGuardAt >= 0);
    ASSERT_TRUE(clearFrameAt > summaryFrameTelemetryReadAt);
    ASSERT_TRUE(clearFrameAt > foregroundGuardAt);
    ASSERT_TRUE(clearTimelineAt > summaryFrameTelemetryReadAt);
    ASSERT_TRUE(clearTimelineAt > foregroundGuardAt);
}

TEST(ContactSheetCaptureWiring, MeasuredSessionMarkerIsLoggedOnceRightAfterTheMeasuredPlayTrigger)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_playbackSmokeMeasuredSessionLogged = false;")));

    // HARDENING (CUDA-PLAYBACK-CONTACT-SHEET-2): the marker must now be logged from
    // runGuiPlaybackSmoke() itself, immediately after the trigger() that opens the MEASURED
    // session -- not from finishPlaybackSmokeTelemetry() on a "play-stop", which a Look Assist
    // Auto-warmup settle or an in-loop clip-lifecycle-stress Play toggle could reach first and
    // mislabel. It must therefore sit strictly between the measured play trigger and the timed
    // measurement while() loop -- never inside a warmup/stress block above it, and never after
    // the loop has already started consuming the measured window.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int measuredTriggerAt = smokeBody.indexOf(QStringLiteral("forcePlaybackSmokeWindowForeground();"));
    const int markerGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !m_playbackSmokeMeasuredSessionLogged )"), measuredTriggerAt);
    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("while( playbackClock.elapsed() < durationMs && ui->actionPlay->isChecked() )"));
    ASSERT_TRUE(measuredTriggerAt >= 0);
    ASSERT_TRUE(markerGuardAt > measuredTriggerAt);
    ASSERT_TRUE(timedLoopAt > markerGuardAt);

    const QString markerBlock = smokeBody.mid(markerGuardAt, 400);
    ASSERT_TRUE(markerBlock.contains(QStringLiteral("m_playbackSmokeMeasuredSessionLogged = true;")));
    ASSERT_TRUE(markerBlock.contains(QStringLiteral("\"playback_smoke.measured_session id=%1\"")));

    // The old first-"play-stop" binding must be gone from finishPlaybackSmokeTelemetry -- a
    // leftover copy there would double-log the marker (harmless for the FIRST such call since
    // the flag is already set, but proof the relocation actually happened, not just an addition).
    const QString finishBody = sliceBetween(source,
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
    ASSERT_FALSE(finishBody.isEmpty());
    ASSERT_FALSE(finishBody.contains(QStringLiteral("m_playbackSmokeMeasuredSessionLogged = true;")));
    ASSERT_FALSE(finishBody.contains(QStringLiteral("qstrcmp( reason, \"play-stop\" )")));
}

// --- CUDA-PLAYBACK-CONTACT-SHEET-2 (BLOCKER fix: looped span collapse) -----------------------

TEST(ContactSheetCaptureWiring, WrapFlagExistsResetsPerSessionAndIsSetWhenTheTimelineGoesBackwards)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_playbackSmokeWrapped = false;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // beginPlaybackSmokeTelemetry() must reset the flag for every new session, right alongside
    // the first/last-presented-frame fields it already resets -- otherwise a wrap detected in
    // an EARLIER session (e.g. a warmup) would wrongly force the wrapped span path for a later,
    // non-looping measured session in the same process.
    const QString beginBody = sliceBetween(source,
        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"),
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    ASSERT_FALSE(beginBody.isEmpty());
    const int resetLastFrameAt = beginBody.indexOf(QStringLiteral("m_playbackSmokeLastPresentedFrame = -1;"));
    const int resetWrappedAt = beginBody.indexOf(QStringLiteral("m_playbackSmokeWrapped = false;"));
    ASSERT_TRUE(resetLastFrameAt >= 0);
    ASSERT_TRUE(resetWrappedAt > resetLastFrameAt);

    // notePlaybackSmokePresentedFrame() must set the flag when the presented-frame transition
    // qualifies as a loop wrap (playback_frame_range::isContactSheetLoopWrapTransition -- see
    // the round-2 LOOP-WRAP-QUALIFICATION test below), and must check this BEFORE
    // m_playbackSmokeLastPresentedFrame is overwritten with the new value, or the comparison
    // would always see the frame compared against itself.
    const QString noteBody = sliceBetween(source,
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("));
    ASSERT_FALSE(noteBody.isEmpty());
    const int wrapCheckAt = noteBody.indexOf(
        QStringLiteral("playback_frame_range::isContactSheetLoopWrapTransition("));
    const int setWrappedAt = noteBody.indexOf(QStringLiteral("m_playbackSmokeWrapped = true;"), wrapCheckAt);
    const int overwriteLastFrameAt = noteBody.indexOf(
        QStringLiteral("m_playbackSmokeLastPresentedFrame = static_cast<int>( displayFrame );"));
    ASSERT_TRUE(wrapCheckAt >= 0);
    ASSERT_TRUE(setWrappedAt > wrapCheckAt);
    ASSERT_TRUE(overwriteLastFrameAt > setWrappedAt);
}

// --- CUDA-PLAYBACK-CONTACT-SHEET-2 round 2 (BLOCKER: wrapped-span final target unreachable in
// drop-frame looping playback; HARDENING: LOOP-WRAP-QUALIFICATION) -----------------------------

TEST(ContactSheetCaptureWiring, LoopWrapDetectionIsQualifiedByLoopStateAndJumpSize)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString noteBody = sliceBetween(source,
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("));
    ASSERT_FALSE(noteBody.isEmpty());

    // A bare "went backward" test also fires for a backward scrub or stress seek with Loop off,
    // or to an arbitrary earlier frame -- the call must pass the live Loop state and both cut
    // spinboxes so the pure helper can require an actual loop-width-sized jump.
    const int callAt = noteBody.indexOf(
        QStringLiteral("playback_frame_range::isContactSheetLoopWrapTransition("));
    ASSERT_TRUE(callAt >= 0);
    const QString callBlock = noteBody.mid(callAt, 400);
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->actionLoop->isChecked()")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->spinBoxCutIn->value() - 1")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->spinBoxCutOut->value() - 1")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("m_playbackSmokeLastPresentedFrame")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackFrameRange.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline bool isContactSheetLoopWrapTransition(")));
    // The un-qualified pre-round-2 shape must actually be gone, not just supplemented.
    ASSERT_FALSE(noteBody.contains(
        QStringLiteral("static_cast<int>( displayFrame ) < m_playbackSmokeLastPresentedFrame")));
}

TEST(ContactSheetCaptureWiring, PlaybackModeCaptureForcesDropFrameModeOffForTheDurationOfTheReplay)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // BLOCKER fix: drop-frame mode must be captured and forced off BEFORE the un-timed capture
    // while() loop starts (so playbackHandling takes its deterministic, single-frame-advance
    // branch for the whole replay), and restored to its prior state AFTER the loop and the
    // idle-drain wait that follows it, before the timeout/error bookkeeping below reads
    // m_contactSheetCaptureFramesWritten.
    const int activeAt = smokeBody.indexOf(QStringLiteral("m_contactSheetCaptureActive = true;"));
    ASSERT_TRUE(activeAt >= 0);
    const int forceOffAt = smokeBody.indexOf(
        QStringLiteral("const bool contactSheetDropFrameModeBefore = ui->actionDropFrameMode->isChecked();"),
        activeAt);
    ASSERT_TRUE(forceOffAt > activeAt);
    const int setCheckedFalseAt = smokeBody.indexOf(
        QStringLiteral("ui->actionDropFrameMode->setChecked( false );"), forceOffAt);
    ASSERT_TRUE(setCheckedFalseAt > forceOffAt);
    const int whileLoopAt = smokeBody.indexOf(QStringLiteral("while( m_contactSheetCaptureActive"), setCheckedFalseAt);
    ASSERT_TRUE(whileLoopAt > setCheckedFalseAt);
    const int idleDrainAt = smokeBody.indexOf(
        QStringLiteral("!m_pRenderThread->isIdle();"), whileLoopAt);
    ASSERT_TRUE(idleDrainAt > whileLoopAt);
    const int setCheckedTrueAt = smokeBody.indexOf(
        QStringLiteral("ui->actionDropFrameMode->setChecked( true );"), idleDrainAt);
    ASSERT_TRUE(setCheckedTrueAt > idleDrainAt);
    const int errorBookkeepingAt = smokeBody.indexOf(
        QStringLiteral("if( m_contactSheetCaptureActive && m_contactSheetCaptureError.isEmpty() )"),
        setCheckedTrueAt);
    ASSERT_TRUE(errorBookkeepingAt > setCheckedTrueAt);

    // Both the force-off and the restore must be gated on the SAME captured prior state, so a
    // run that started with drop-frame mode already off never toggles the action at all.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( contactSheetDropFrameModeBefore )\n            {\n                ui->actionDropFrameMode->setChecked( false );")));
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( contactSheetDropFrameModeBefore )\n            {\n                ui->actionDropFrameMode->setChecked( true );")));

    // The seek-mode branch never plays at all, so it must not be touched by this fix.
    const QString seekBranch = sliceBetween(smokeBody,
        QStringLiteral("if( options.contactSheetSeekMode )"),
        QStringLiteral("m_contactSheetCaptureDir = options.contactSheetDir;"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionDropFrameMode")));
}

TEST(ContactSheetCaptureWiring, ContactSheetTargetFramesAreComputedByTheSharedPureHelper)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // Both capture modes share ONE target list built from the same real, executable-tested
    // function (test_playback_frame_range.cpp), rather than each re-deriving the fraction
    // arithmetic inline where it could silently drift between the two modes.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("playback_frame_range::contactSheetTargetFrame(\n                i, options.contactSheetFrames, sheetStartFrame, sheetEndFrame )")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackFrameRange.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline int contactSheetTargetFrame(")));
}

TEST(ContactSheetCaptureWiring, WrappedSpanIsOverriddenToTheLoopCutInCutOutRangeNotLastPresentedFrame)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // The span snapshot reads m_playbackSmokeWrapped and, when set, overrides BOTH
    // contactSheetStartFrame and contactSheetEndFrame from the Loop action's own cutIn/cutOut
    // spinboxes -- deterministic settings values, never a presented-frame artifact that can
    // differ between two hosts measuring the same clip.
    const int wrappedReadAt = smokeBody.indexOf(
        QStringLiteral("const bool contactSheetSpanWrapped = m_playbackSmokeWrapped;"));
    ASSERT_TRUE(wrappedReadAt >= 0);
    const int overrideGuardAt = smokeBody.indexOf(
        QStringLiteral("if( contactSheetSpanWrapped )"), wrappedReadAt);
    ASSERT_TRUE(overrideGuardAt > wrappedReadAt);
    const QString overrideBlock = smokeBody.mid(overrideGuardAt, 700);
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("ui->spinBoxCutIn->value()")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("ui->spinBoxCutOut->value()")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("contactSheetStartFrame = contactSheetLoopCutInFrame;")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("contactSheetEndFrame = contactSheetLoopCutOutFrame;")));

    // The override must run BEFORE sheetStartFrame/sheetEndFrame (the values actually used to
    // build the evenly spaced target-frame list) are computed from contactSheetStartFrame/
    // contactSheetEndFrame -- otherwise the wrap override would be silently discarded.
    const int sheetStartComputedAt = smokeBody.indexOf(QStringLiteral("const int sheetStartFrame = qBound("));
    ASSERT_TRUE(sheetStartComputedAt > overrideGuardAt);
}

TEST(ContactSheetCaptureWiring, PlaybackModeSidecarAndDoneLineCarrySpanStartEndAndWrappedFields)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // noteContactSheetPresentedFrame() (the default playback-mode capture path) stamps every
    // sidecar with the span the job-level composer/owner needs to know what was actually
    // covered, using the member fields runGuiPlaybackSmoke set up once for the whole pass.
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_start\"), m_contactSheetCaptureStartFrame );")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), m_contactSheetCaptureEndFrame );")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), m_contactSheetCaptureWrapped );")));

    // The seek-mode branch (runs inline inside runGuiPlaybackSmoke, not through the hook above)
    // stamps the same three fields from its own local sheetStartFrame/sheetEndFrame/
    // contactSheetSpanWrapped -- a reader must see the same span regardless of which mode wrote
    // a given sidecar.
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const QString seekBranch = sliceBetween(smokeBody,
        QStringLiteral("if( options.contactSheetSeekMode )"),
        QStringLiteral("m_contactSheetCaptureDir = options.contactSheetDir;"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_start\"), sheetStartFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), sheetEndFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), contactSheetSpanWrapped );")));

    // The else-branch (playback mode setup) must hand the same span down to the member fields
    // the hook above reads, so both modes really do agree.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("m_contactSheetCaptureEndFrame = sheetEndFrame;")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("m_contactSheetCaptureWrapped = contactSheetSpanWrapped;")));

    // The job-level DONE line (gui_smoke.contact_sheet) also carries the wrapped flag, so a
    // reader who only has the raw log (not the per-frame sidecars) can still see it.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("start_frame=%4 end_frame=%5 wrapped=%6")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(".arg( bool01( contactSheetSpanWrapped ) )")));
}
