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

TEST(ContactSheetCaptureWiring, FileWritingRunsStrictlyAfterPlaybackStopsAndAfterTheTimedLoopCloses)
{
    // CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass GRABS happen inside the measured interval (that
    // is the point: they are what played), but every PNG encode, sidecar write and the seek-mode
    // capture still run in the block below, strictly after the measured Play is stopped.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
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
    // Every PNG encode in the smoke body sits in that block.
    const int firstSaveAt = smokeBody.indexOf(QStringLiteral(".save( pngPath, \"PNG\" )"));
    ASSERT_TRUE(firstSaveAt > captureGuardAt);

    // finishPlaybackSmokeTelemetry() runs synchronously inside on_actionPlay_toggled(false),
    // which setChecked(false) above triggers before the write block, so the fps/swap-cadence
    // session is closed before any file is written.
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
        // LOOK-ASSIST-SCENE-CLASSIFY-1 r1c: where the balance came from, so a sheet can assert it.
        "look_assist_wb_source", "look_assist_wb_decision",
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

// --- CONTACT-SHEET-PLAYBACK-PARITY-1: the default sheet is captured DURING the measured Play ----

static QString smokeBodyOf(const QString & source)
{
    return sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
}

static const char * kInPassArmMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: arm the in-pass contact sheet on THIS measured Play";
static const char * kInPassWriteMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: write the in-pass grabs";
static const char * kSeekMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: seek capture (explicit, labelled alternative)";

TEST(ContactSheetCaptureWiring, InPassCaptureIsArmedOnTheMeasuredPlayBeforeTheTimedLoop)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());

    const int measuredPlayAt = smokeBody.indexOf(QStringLiteral("programmaticPlay( \"gui-smoke-measured\""));
    const int clockRestartAt = smokeBody.indexOf(QStringLiteral("playbackClock.restart();"), measuredPlayAt);
    const int armAt = smokeBody.indexOf(QString::fromLatin1(kInPassArmMarker));
    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
    ASSERT_TRUE(measuredPlayAt >= 0);
    ASSERT_TRUE(clockRestartAt > measuredPlayAt);
    ASSERT_TRUE(armAt > clockRestartAt);
    ASSERT_TRUE(timedLoopAt > armAt);

    const QString armEnd = QStringLiteral("m_contactSheetCaptureActive = true;");
    const int armEndAt = smokeBody.indexOf(armEnd, armAt);
    ASSERT_TRUE(armEndAt > armAt);
    ASSERT_TRUE(timedLoopAt > armEndAt);
    const QString armBlock = smokeBody.mid(armAt, armEndAt + armEnd.size() - armAt);
    ASSERT_TRUE(armBlock.contains(QStringLiteral(
        "if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 && !options.contactSheetSeekMode )")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("playback_frame_range::contactSheetInPassTargetMs(")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("m_contactSheetCaptureClock.start();")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("m_contactSheetCaptureActive = true;")));
    // Arming never plays anything itself.
    ASSERT_FALSE(armBlock.contains(QStringLiteral("programmaticPlay(")));
    ASSERT_FALSE(armBlock.contains(QStringLiteral("actionPlay")));
}

TEST(ContactSheetCaptureWiring, InPassSheetAddsNoReplayAndNoSecondPlay)
{
    // Owner rule 2026-09-30: never replay. The ONE programmatic Play in the smoke body is the
    // measured one; the old replay pass, its restart machinery and its refusal are all gone.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_EQ(1, smokeBody.count(QStringLiteral("programmaticPlay(")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("REPLAY_REFUSED: the playback-mode contact sheet")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("contactSheetMaxRestarts")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("gui-smoke-contact-sheet-restart")));

    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionPlay")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionLoop")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("seekAndSettleLoadedClip(")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("grabPresentedFramebufferIfActive(")));   // writes, never grabs
}

TEST(ContactSheetCaptureWiring, InPassHookOnlyReadsBackAndTimesTheGrabInsideTheMeasuredInterval)
{
    // The hook runs on the GUI thread inside the measured interval. It may only do the readback
    // (timed) and keep the image; PNG encode and file I/O wait until the Play is stopped.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetCaptureClock.elapsed()")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetCaptureTargetMs")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QElapsedTimer grabTimer;")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"grab_ms\")")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetPendingGrabs.append(")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral(".save(")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("QFile")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("toJson(")));
}

TEST(ContactSheetCaptureWiring, InPassWriterRecordsTheGrabCostAndWhetherEachGrabFellInTheMeasuredInterval)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_TRUE(writeBranch.contains(QStringLiteral(".save( pngPath, \"PNG\" )")));
    ASSERT_TRUE(writeBranch.contains(QStringLiteral("QStringLiteral(\"grab_in_measured_interval\")")));
    ASSERT_TRUE(writeBranch.contains(QStringLiteral("playback_frame_range::fpsExcludingGrabCost(")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("grab_total_ms=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("grabs_in_measured_interval=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("fps_excluding_grabs=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("playback_path=%")));
}

TEST(ContactSheetCaptureWiring, PairedSeekSheetIsOptInWritesItsOwnDirAndIsLabelledNotPlayback)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("QString contactSheetSeekDir;")));
    const QString mainSource = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(mainSource.contains(QStringLiteral("QStringLiteral(\"contact-sheet-seek-dir\")")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker), QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("options.contactSheetSeekMode ? options.contactSheetDir : options.contactSheetSeekDir")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), false );")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionPlay->trigger")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("programmaticPlay(")));
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

    // CONTACT-SHEET-PLAYBACK-PARITY-1: the only capture that needs the per-present hook is the
    // in-pass one, so the flag is set where that capture is armed (seek mode never grabs there).
    const int guardAt = smokeBody.indexOf(QStringLiteral(
        "if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 && !options.contactSheetSeekMode )"));
    const int setTrueAt = smokeBody.indexOf(
        QStringLiteral("m_contactSheetOptionsPresent = true;"), guardAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(setTrueAt > guardAt);
    ASSERT_TRUE(setTrueAt - guardAt < 2500);

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
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
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

    // A frame presented while Play is not checked (a seek, a paused redraw) is never kept as
    // playback_path=true: the hook refuses it strictly BEFORE the readback.
    const int activeGuardAt = hookBody.indexOf(QStringLiteral("if( !m_contactSheetCaptureActive ) return;"));
    const int checkedGuardAt = hookBody.indexOf(QStringLiteral("if( !ui->actionPlay->isChecked() ) return;"));
    const int grabAt = hookBody.indexOf(QStringLiteral("GpuDisplayWindow::grabPresentedFramebufferIfActive("));
    ASSERT_TRUE(activeGuardAt >= 0);
    ASSERT_TRUE(checkedGuardAt > activeGuardAt);
    ASSERT_TRUE(grabAt > checkedGuardAt);
}

TEST(ContactSheetCaptureWiring, B2_ContactSheetStateNeverGatesPlaybackSmokeTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString togglePlayBody = sliceBetween(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(togglePlayBody.isEmpty());

    // The guard that suppressed beginPlaybackSmokeTelemetry() existed only for the capture
    // REPLAY's restarts. There is no replay (CONTACT-SHEET-PLAYBACK-PARITY-1 captures during
    // the measured Play), so a Play toggle inside the measured loop (clip-lifecycle stress) must
    // open its telemetry session exactly as it does without a contact sheet.
    const int checkedBranchAt = togglePlayBody.indexOf(QStringLiteral("if( checked )"));
    ASSERT_TRUE(checkedBranchAt >= 0);
    ASSERT_FALSE(togglePlayBody.contains(
        QStringLiteral("if( !m_contactSheetCaptureActive ) beginPlaybackSmokeTelemetry();")));
    const int bareCallAt = togglePlayBody.indexOf(
        QStringLiteral("\n        beginPlaybackSmokeTelemetry();\n"), checkedBranchAt);
    ASSERT_TRUE(bareCallAt > checkedBranchAt);
}

TEST(ContactSheetCaptureWiring, H1_SeekAndSettleKeepsItsBoundedTimeoutParameterAndNoRestartSeekRemains)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // seekAndSettleLoadedClip keeps its optional timeoutMs parameter (default 8000) for the stress seeks
    // and the seek-mode capture; the contact-sheet RESTART call site that used to pass what remained of
    // the overall deadline is gone with the replay it served (PLAYBACK-CLIP-LENGTH-ENFORCE-2).
    const int lambdaAt = smokeBody.indexOf(QStringLiteral("auto seekAndSettleLoadedClip = [&]("));
    ASSERT_TRUE(lambdaAt >= 0);
    const QString lambdaHeader = smokeBody.mid(lambdaAt, 400);
    ASSERT_TRUE(lambdaHeader.contains(QStringLiteral("int timeoutMs = 8000")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("restartSeekTimeoutMs")));
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
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
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
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
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

TEST(ContactSheetCaptureWiring, PlaybackModeNoLongerTouchesDropFrameModeBecauseThereIsNoReplay)
{
    // The drop-frame force-off existed only to make the capture REPLAY deterministic. The replay is gone
    // (PLAYBACK-CLIP-LENGTH-ENFORCE-2), so nothing in the smoke body toggles drop-frame mode for it.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    // The in-pass (default) capture rides the measured Play as configured: neither its arm block
    // nor its write block touches drop-frame mode.
    const int armAt = smokeBody.indexOf(QString::fromLatin1(kInPassArmMarker));
    ASSERT_TRUE(armAt >= 0);
    const QString armBlock = smokeBody.mid(armAt, 2500);
    ASSERT_FALSE(armBlock.contains(QStringLiteral("actionDropFrameMode")));
    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionDropFrameMode")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("contactSheetDropFrameModeBefore")));

    // The seek-mode branch never plays at all, so it must not be touched by this either.
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionDropFrameMode")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionPlay->trigger")));
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
    // CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass grab happens before the measured span has
    // ended, so the writer (after the stop) stamps the measured span's end and wrap flag.
    const QString inPassSmokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    const QString writeBranch = sliceBetween(inPassSmokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_TRUE(writeBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), sheetEndFrame );")));
    ASSERT_TRUE(writeBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), contactSheetSpanWrapped );")));

    // The seek-mode branch (runs inline inside runGuiPlaybackSmoke, not through the hook above)
    // stamps the same three fields from its own local sheetStartFrame/sheetEndFrame/
    // contactSheetSpanWrapped -- a reader must see the same span regardless of which mode wrote
    // a given sidecar.
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_start\"), sheetStartFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), sheetEndFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), contactSheetSpanWrapped );")));

    // The job-level DONE line (gui_smoke.contact_sheet) also carries the wrapped flag, so a
    // reader who only has the raw log (not the per-frame sidecars) can still see it.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("start_frame=%4 end_frame=%5 wrapped=%6")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(".arg( bool01( contactSheetSpanWrapped ) )")));
}
